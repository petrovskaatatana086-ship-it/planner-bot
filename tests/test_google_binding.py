"""Binding changes and real OAuth/bot adapters with isolated SQLite and fake network."""
import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import web
from app.bot import handlers
from app.config import Settings
from app.db.base import Base
from app.db.models import AuditLog, BookingRequest, CalendarSync, Meeting, OAuthCredential, OperationResult, OutboxEvent, OwnerProfile, User
from app.db.session import make_engine
from app.security import generate_encryption_key
from app.services.booking import BookingError
from app.services.google_binding import GoogleBindingCommands


class GoogleBindingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path.cwd()/f".test_binding_{uuid.uuid4().hex}.db"
        self.settings = Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
            DATABASE_URL=f"sqlite+aiosqlite:///{self.path.as_posix()}", GOOGLE_CLIENT_ID="synthetic",
            GOOGLE_CLIENT_SECRET="synthetic", GOOGLE_REDIRECT_URI="https://example.com/callback",
            OAUTH_ENCRYPTION_KEY=generate_encryption_key())
        self.engines = [make_engine(self.settings), make_engine(self.settings)]
        self.sessions = [async_sessionmaker(e, expire_on_commit=False) for e in self.engines]
        async with self.engines[0].begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.commands = [GoogleBindingCommands(s, owner_telegram_id=777) for s in self.sessions]
        self.owner = await self.commands[0].ensure_telegram_user(telegram_id=777, full_name="Owner")
        self.client = await self.commands[0].ensure_telegram_user(telegram_id=101, full_name="Client")
        self.state = f"{self.owner.id}:synthetic-nonce"

    async def asyncTearDown(self):
        for engine in self.engines:
            await engine.dispose()
        self.path.unlink(missing_ok=True)

    async def profile(self):
        async with self.sessions[0]() as session:
            return await session.scalar(select(OwnerProfile))

    async def credential(self):
        async with self.sessions[0]() as session:
            return await session.scalar(select(OAuthCredential))

    async def begin(self, state=None):
        await self.commands[0].begin_oauth(actor_id=self.owner.id, oauth_state=state or self.state)

    async def complete(self, **overrides):
        data = dict(actor_id=self.owner.id, oauth_state=self.state, expected_version=(await self.profile()).version,
            encrypted_access_token="encrypted-access", encrypted_refresh_token="encrypted-refresh",
            expires_at=None, google_email="owner@example.com")
        data.update(overrides)
        await self.commands[0].complete_oauth(**data)

    async def connected(self):
        await self.begin()
        await self.complete()

    async def choose(self, *, index=0, **overrides):
        data = dict(actor_id=self.owner.id, command_id="choose", calendar_id="calendar-a", expected_version=2)
        data.update(overrides)
        return await self.commands[index].select_calendar(**data)

    async def meeting(self, *, sync=False, legacy=False, cancelled=False):
        now = datetime.now(timezone.utc)
        async with self.sessions[0]() as session:
            request = BookingRequest(client_id=self.client.id, title="Synthetic", start_at=now,
                end_at=now+timedelta(hours=1), status="approved")
            session.add(request)
            await session.flush()
            meeting = Meeting(client_id=self.client.id, booking_request_id=request.id, title="Synthetic",
                start_at=request.start_at, end_at=request.end_at,
                status="cancelled_by_owner" if cancelled else "confirmed",
                google_event_id="legacy" if legacy else None)
            session.add(meeting)
            await session.flush()
            if sync:
                session.add(CalendarSync(meeting_id=meeting.id, calendar_id="calendar-a", environment="test",
                    deterministic_event_id="event", conference_request_id="conference", desired_version=1,
                    applied_version=0, state="pending"))
            await session.commit()

    def request(self, **query):
        return SimpleNamespace(query=query or dict(code="synthetic-code", state=self.state),
            app=dict(settings=self.settings, session_factory=self.sessions[0], bot=SimpleNamespace(send_message=AsyncMock())))

    def tokens(self):
        return SimpleNamespace(access_token="synthetic-access", refresh_token="synthetic-refresh", expires_at=None)

    async def test_selection_version_replay_and_concurrent_same_key(self):
        await self.connected()
        first, second = await asyncio.gather(self.choose(), self.choose(index=1))
        self.assertEqual(first, second)
        self.assertEqual(first["version"], 3)
        await self.choose(command_id="next", calendar_id="calendar-b", expected_version=3)
        self.assertEqual(await self.choose(), first)
        self.assertEqual((await self.profile()).calendar_id, "calendar-b")
        async with self.sessions[0]() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(OperationResult)), 2)
        with self.assertRaises(BookingError) as error:
            await self.choose(command_id="stale")
        self.assertEqual(error.exception.code, "STALE_VERSION")

    async def test_active_meetings_pin_calendar_and_account(self):
        await self.connected()
        await self.choose()
        await self.meeting()
        with self.assertRaises(BookingError) as error:
            await self.choose(command_id="switch", calendar_id="calendar-b", expected_version=3)
        self.assertEqual(error.exception.code, "CALENDAR_BINDING_IN_USE")
        await self.begin()
        with self.assertRaises(BookingError) as error:
            await self.complete(google_email="another@example.com")
        self.assertEqual(error.exception.code, "CALENDAR_BINDING_IN_USE")
        self.assertEqual((await self.credential()).encrypted_access_token, "encrypted-access")
        self.assertEqual((await self.profile()).calendar_id, "calendar-a")

    async def test_cancelled_legacy_events_still_pin_binding(self):
        await self.connected()
        await self.meeting(legacy=True, cancelled=True)
        with self.assertRaises(BookingError) as error:
            await self.choose()
        self.assertEqual(error.exception.code, "CALENDAR_BINDING_IN_USE")

    async def test_pending_sync_pins_binding_and_same_calendar_is_noop(self):
        await self.connected()
        await self.choose()
        await self.meeting(sync=True, cancelled=True)
        result = await self.choose(command_id="same", expected_version=3)
        self.assertEqual(result["version"], 3)
        with self.assertRaises(BookingError) as error:
            await self.choose(command_id="switch", calendar_id="calendar-b", expected_version=3)
        self.assertEqual(error.exception.code, "CALENDAR_BINDING_IN_USE")
        async with self.sessions[0]() as session:
            sync = await session.scalar(select(CalendarSync))
            self.assertEqual((sync.calendar_id, sync.state, sync.applied_version), ("calendar-a", "pending", 0))

    async def test_reconnect_same_account_preserves_calendar_and_refresh(self):
        await self.connected()
        await self.choose()
        await self.meeting(sync=True)
        await self.begin()
        self.assertEqual((await self.credential()).status, "active")
        await self.complete(encrypted_access_token="new-access", encrypted_refresh_token=None,
                            google_email="OWNER@example.com")
        self.assertEqual((await self.profile()).calendar_id, "calendar-a")
        self.assertEqual((await self.profile()).version, 4)
        self.assertEqual((await self.credential()).encrypted_refresh_token, "encrypted-refresh")

    async def test_new_account_clears_calendar_and_requires_own_refresh(self):
        await self.connected()
        await self.choose()
        await self.begin()
        with self.assertRaises(BookingError) as error:
            await self.complete(google_email="new@example.com", encrypted_refresh_token=None)
        self.assertEqual(error.exception.code, "GOOGLE_REFRESH_REQUIRED")
        await self.complete(google_email="new@example.com", encrypted_refresh_token="new-refresh")
        self.assertIsNone((await self.profile()).calendar_id)
        self.assertEqual((await self.credential()).encrypted_refresh_token, "new-refresh")

    async def test_failed_reconnect_does_not_disable_active_connection(self):
        await self.connected()
        await self.choose()
        await self.begin()
        self.assertTrue(await self.commands[0].fail_oauth(actor_id=self.owner.id, oauth_state=self.state))
        self.assertFalse(await self.commands[1].fail_oauth(actor_id=self.owner.id, oauth_state=self.state))
        self.assertEqual((await self.credential()).status, "active")
        self.assertEqual((await self.profile()).version, 3)
        self.assertEqual((await self.profile()).google_status, "active")

    async def test_old_state_cannot_complete_or_fail_new_flow(self):
        await self.begin()
        await self.begin(state="new-state")
        self.assertFalse(await self.commands[0].fail_oauth(actor_id=self.owner.id, oauth_state=self.state))
        with self.assertRaises(BookingError) as error:
            await self.complete()
        self.assertEqual(error.exception.code, "INVALID_OAUTH_STATE")
        self.assertEqual((await self.credential()).oauth_state, "new-state")

    async def test_blocked_owner_and_role_spoof_fail_before_network(self):
        await self.begin()
        async with self.sessions[0]() as session:
            (await session.get(User, self.owner.id)).status = "blocked"
            (await session.get(User, self.client.id)).role = "owner"
            await session.commit()
        with patch("app.web.exchange_code_for_tokens", new_callable=AsyncMock) as exchange:
            response = await web.google_oauth_callback(self.request())
            self.assertEqual(response.status, 400)
            exchange.assert_not_awaited()
        with self.assertRaises(BookingError) as error:
            await self.commands[0].begin_oauth(actor_id=self.client.id, oauth_state="spoof")
        self.assertEqual(error.exception.code, "FORBIDDEN")

    async def test_invalid_callback_has_no_network_or_failure_write(self):
        await self.begin()
        with patch("app.web.exchange_code_for_tokens", new_callable=AsyncMock) as exchange:
            response = await web.google_oauth_callback(self.request(code="code", state=f"{self.owner.id}:wrong"))
            self.assertEqual(response.status, 400)
            exchange.assert_not_awaited()
        self.assertEqual((await self.credential()).oauth_state, self.state)

    async def test_real_callback_consumes_state_once_and_does_not_store_secrets_in_audit(self):
        await self.begin()
        with patch("app.web.exchange_code_for_tokens", new_callable=AsyncMock, return_value=self.tokens()) as exchange, \
             patch("app.web.fetch_google_account_email", new_callable=AsyncMock, return_value="owner@example.com"):
            self.assertEqual((await web.google_oauth_callback(self.request())).status, 200)
            self.assertEqual((await web.google_oauth_callback(self.request())).status, 400)
            self.assertEqual(exchange.await_count, 1)
        async with self.sessions[0]() as session:
            audits = (await session.scalars(select(AuditLog))).all()
            self.assertTrue(all(row.metadata_json is None for row in audits))
            self.assertEqual(await session.scalar(select(func.count()).select_from(OperationResult)), 0)

    async def test_network_failure_preserves_active_tokens_and_sanitizes_error(self):
        await self.connected()
        await self.begin()
        request = self.request()
        with patch("app.web.exchange_code_for_tokens", new_callable=AsyncMock, side_effect=ValueError("SENSITIVE")):
            response = await web.google_oauth_callback(request)
        self.assertEqual(response.status, 400)
        self.assertNotIn("SENSITIVE", response.text)
        self.assertNotIn("SENSITIVE", request.app["bot"].send_message.call_args.args[1])
        self.assertEqual((await self.credential()).status, "active")
        self.assertEqual((await self.credential()).last_error, "GOOGLE_OAUTH_FAILED")

    async def test_profile_changes_during_exchange_fail_without_writer_lock(self):
        await self.begin()
        async def exchange(settings, code):
            # Independent engine must be able to write while the network is awaited.
            await self.commands[1].apply_settings_message(actor_id=self.owner.id,
                command_id="during-network", changes={"daily_limit": 12})
            return self.tokens()
        with patch("app.web.exchange_code_for_tokens", side_effect=exchange), \
             patch("app.web.fetch_google_account_email", new_callable=AsyncMock, return_value="owner@example.com"):
            self.assertEqual((await web.google_oauth_callback(self.request())).status, 400)
        self.assertIsNone((await self.credential()).encrypted_access_token)
        self.assertEqual((await self.profile()).daily_limit, 12)

    async def test_bot_selection_uses_captured_version(self):
        await self.connected()
        state = AsyncMock()
        state.get_data.return_value = dict(google_calendars=[dict(id="calendar-a", summary="Synthetic")],
                                           google_binding_version=2)
        message = SimpleNamespace(from_user=SimpleNamespace(id=777, full_name="Owner", username=None),
            chat=SimpleNamespace(id=777), message_id=10, text="1", answer=AsyncMock())
        await self.commands[1].apply_settings_message(actor_id=self.owner.id,
            command_id="change", changes={"daily_limit": 12})
        await handlers.save_google_calendar_selection(message, state, self.settings, self.sessions[0])
        self.assertIsNone((await self.profile()).calendar_id)
        state.clear.assert_not_awaited()
        self.assertIn("изменились", message.answer.call_args.args[0])

    async def test_outbox_without_sync_still_pins_binding(self):
        await self.connected()
        async with self.sessions[0]() as session:
            session.add(OutboxEvent(event_id=uuid.uuid4().hex, entity_id=99, entity_version=1,
                channel="google", kind="meeting.sync", state="processing", attempts=1,
                next_attempt_at=datetime.now(timezone.utc)))
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.choose()
        self.assertEqual(error.exception.code, "CALENDAR_BINDING_IN_USE")

    async def test_selection_reused_key_and_blocked_replay(self):
        await self.connected()
        await self.choose()
        with self.assertRaises(BookingError) as error:
            await self.choose(calendar_id="calendar-b")
        self.assertEqual(error.exception.code, "IDEMPOTENCY_KEY_REUSED")
        async with self.sessions[0]() as session:
            (await session.get(User, self.owner.id)).status = "blocked"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.choose()
        self.assertEqual(error.exception.code, "ACCOUNT_BLOCKED")

    async def test_denied_callback_preserves_active_connection_and_hides_provider_text(self):
        await self.connected()
        await self.begin()
        response = await web.google_oauth_callback(self.request(error="SENSITIVE", state=self.state))
        self.assertEqual(response.status, 400)
        self.assertNotIn("SENSITIVE", response.text)
        self.assertEqual((await self.credential()).status, "active")
        self.assertIsNone((await self.credential()).oauth_state)

    async def test_new_flow_during_network_cannot_be_overwritten_by_old_callback(self):
        await self.begin()
        async def exchange(settings, code):
            await self.commands[1].begin_oauth(actor_id=self.owner.id, oauth_state="new-flow")
            return self.tokens()
        with patch("app.web.exchange_code_for_tokens", side_effect=exchange), \
             patch("app.web.fetch_google_account_email", new_callable=AsyncMock, return_value="owner@example.com"):
            self.assertEqual((await web.google_oauth_callback(self.request())).status, 400)
        self.assertEqual((await self.credential()).oauth_state, "new-flow")
        self.assertIsNone((await self.credential()).encrypted_access_token)
