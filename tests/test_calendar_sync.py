"""Google integration tests use a fake Calendar and synthetic SQLite only."""
import copy
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.base import Base
from app.db.models import BookingRequest, CalendarSync, Meeting, OAuthCredential, OutboxEvent, OwnerProfile, User
from app.db.session import make_engine
from app.db.transactions import run_sqlite_write
from app.integrations.google_events import GoogleEventGateway, GoogleFailure
from app.security import generate_encryption_key, encrypt_secret
from app.services.booking_deliveries import BookingDeliveries
from app.services.calendar_sync import CalendarDeliveries, enqueue_calendar_sync
from app.services.google_tokens import GoogleTokens
from app.services.outbox import DeliveryResult, OutboxWorker


class FakeCalendar:
    def __init__(self):
        self.events = {}
        self.inserts = 0
        self.patches = 0
        self.deletes = 0
        self.lose_insert_reply = False
        self.lose_patch_reply = False
        self.pending_conference = False

    async def get(self, calendar, identifier):
        return copy.deepcopy(self.events.get(identifier))

    async def insert(self, calendar, body):
        self.inserts += 1
        if body["id"] in self.events:
            raise GoogleFailure(409)
        event = copy.deepcopy(body)
        event.update(etag='"one"', status="confirmed", htmlLink="https://calendar.google.com/calendar/event?eid=test")
        if not self.pending_conference:
            event["hangoutLink"] = "https://meet.google.com/abc-defg-hij"
        self.events[event["id"]] = event
        if self.lose_insert_reply:
            self.lose_insert_reply = False
            raise GoogleFailure(0)
        return copy.deepcopy(event)

    async def patch(self, calendar, identifier, body, etag):
        self.patches += 1
        if self.events[identifier]["etag"] != etag:
            raise GoogleFailure(412)
        self.events[identifier].update(copy.deepcopy(body))
        self.events[identifier]["etag"] = '"two"'
        if self.lose_patch_reply:
            self.lose_patch_reply = False
            raise GoogleFailure(0)
        return copy.deepcopy(self.events[identifier])

    async def delete(self, calendar, identifier, etag):
        self.deletes += 1
        self.events.pop(identifier, None)


class CalendarSyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path.cwd() / f".test_calendar_sync_{uuid.uuid4().hex}.db"
        self.settings = Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
            DATABASE_URL=f"sqlite+aiosqlite:///{self.path.as_posix()}")
        self.engine = make_engine(self.settings)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.now = datetime(2026, 10, 21, 7, tzinfo=timezone.utc)
        self.gateway = FakeCalendar()
        self.adapters = CalendarDeliveries(self.sessions, gateway=self.gateway, owner_telegram_id=777,
                                           environment="test", clock=lambda: self.now)
        self.bot = AsyncMock()
        self.notices = BookingDeliveries(self.sessions, settings=self.settings, bot=self.bot,
                                         calendar_environment="test", clock=lambda: self.now)
        self.worker = OutboxWorker(self.sessions, {**self.adapters.handlers(), **self.notices.handlers()},
                                   clock=lambda: self.now)
        async with self.sessions() as session:
            owner = User(telegram_id=777, role="owner", full_name="Owner")
            client = User(telegram_id=101, role="client", full_name="Synthetic", email="test@example.com")
            session.add_all([owner, client])
            await session.flush()
            self.owner_id = owner.id
            profile = OwnerProfile(user_id=owner.id, calendar_id="synthetic-calendar", google_status="active")
            request = BookingRequest(client_id=client.id, title="Synthetic", status="approved", version=2,
                start_at=self.now+timedelta(days=3), end_at=self.now+timedelta(days=3, hours=1))
            session.add_all([profile, request])
            await session.flush()
            meeting = Meeting(booking_request_id=request.id, client_id=client.id, title=request.title,
                start_at=request.start_at, end_at=request.end_at, status="confirmed", version=1)
            session.add(meeting)
            await session.flush()
            self.meeting_id = meeting.id
            await enqueue_calendar_sync(session, meeting=meeting, profile=profile, environment="test", now=self.now)
            await session.commit()
        async with self.sessions() as session:
            sync = await session.get(CalendarSync, self.meeting_id)
            self.event_id = sync.deterministic_event_id

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.path.unlink(missing_ok=True)

    async def state(self):
        async with self.sessions() as session:
            return await session.get(CalendarSync, self.meeting_id)

    async def change(self, *, cancel=False):
        async def update(session):
            meeting = await session.get(Meeting, self.meeting_id)
            meeting.version += 1
            meeting.status = "cancelled_by_client" if cancel else "rescheduled"
            if not cancel:
                meeting.start_at += timedelta(days=1)
                meeting.end_at += timedelta(days=1)
            profile = await session.scalar(select(OwnerProfile))
            await enqueue_calendar_sync(session, meeting=meeting, profile=profile, environment="test", now=self.now)
        await run_sqlite_write(self.sessions, update)

    async def notice(self):
        async with self.sessions() as session:
            item = OutboxEvent(event_id=uuid.uuid4().hex, entity_id=1, entity_version=2,
                channel="telegram", kind="request.approved", state="pending", attempts=0,
                next_attempt_at=self.now, created_at=self.now)
            session.add(item)
            await session.commit()
            return item.id

    async def test_create_and_notification_after_sync(self):
        await self.notice()
        await self.worker.run_once()
        sync = await self.state()
        self.assertEqual((sync.state, sync.applied_version), ("synced", 1))
        await self.worker.run_once()
        self.bot.send_message.assert_awaited_once()
        self.assertIn("https://meet.google.com/", self.bot.send_message.await_args.args[1])
        self.assertEqual(self.gateway.inserts, 1)

    async def test_timeout_after_remote_insert_does_not_duplicate(self):
        self.gateway.lose_insert_reply = True
        await self.worker.run_once()
        self.assertEqual((await self.state()).state, "pending")
        self.now += timedelta(seconds=5)
        await self.worker.run_once()
        self.assertEqual(self.gateway.inserts, 1)
        self.assertEqual((await self.state()).state, "synced")

    async def test_409_recovered_by_get(self):
        original_get = self.gateway.get
        calls = 0
        async def invisible_once(calendar, identifier):
            nonlocal calls
            calls += 1
            return None if calls == 1 else await original_get(calendar, identifier)
        snapshot = await self.adapters._snapshot(await self.worker.claim())
        body = self.adapters._body(snapshot)
        body["id"] = self.event_id
        await self.gateway.insert(snapshot.calendar_id, body)
        # Let the first lease expire; the same persisted event ID is reused.
        self.now += timedelta(seconds=91)
        self.gateway.get = invisible_once
        await self.worker.run_once()
        self.assertEqual((await self.state()).state, "synced")
        self.assertEqual(len(self.gateway.events), 1)

    async def test_foreign_event_is_never_overwritten(self):
        self.gateway.events[self.event_id] = {"id": self.event_id, "extendedProperties": {"private": {
            "meetly_environment": "production", "meetly_meeting_id": "1", "meetly_version": "1"}}}
        await self.worker.run_once()
        self.assertEqual((await self.state()).last_error_code, "CALENDAR_OWNERSHIP_CONFLICT")
        self.assertEqual(self.gateway.patches, 0)
        self.assertEqual(self.gateway.deletes, 0)

    async def test_reschedule_patch_timeout_reconciles_before_retry(self):
        await self.worker.run_once()
        await self.change()
        self.gateway.lose_patch_reply = True
        await self.worker.run_once()
        self.now += timedelta(seconds=5)
        await self.worker.run_once()
        self.assertEqual(self.gateway.patches, 1)
        self.assertEqual((await self.state()).applied_version, 2)
        self.assertEqual(self.gateway.inserts, 1)

    async def test_manual_move_is_conflict_not_overwritten(self):
        await self.worker.run_once()
        await self.change()
        self.gateway.events[self.event_id]["start"]["dateTime"] = self.now.isoformat()
        await self.worker.run_once()
        self.assertEqual((await self.state()).last_error_code, "CALENDAR_EXTERNAL_EDIT")
        self.assertEqual(self.gateway.patches, 0)

    async def test_cancel_before_create_skips_remote_creation(self):
        await self.change(cancel=True)
        await self.worker.run_once()  # oldest job coalesces to latest cancellation
        self.assertEqual(self.gateway.inserts, 0)
        self.assertEqual((await self.state()).applied_version, 2)
        self.assertEqual((await self.state()).state, "synced")

    async def test_cancel_after_create_deletes_event(self):
        await self.worker.run_once()
        await self.change(cancel=True)
        await self.worker.run_once()
        self.assertNotIn(self.event_id, self.gateway.events)
        self.assertEqual(self.gateway.deletes, 1)

    async def test_new_version_during_network_is_not_marked_synced(self):
        insert = self.gateway.insert
        async def advance(calendar, body):
            result = await insert(calendar, body)
            await self.change(cancel=True)
            return result
        self.gateway.insert = advance
        await self.worker.run_once()
        sync = await self.state()
        self.assertEqual((sync.state, sync.applied_version, sync.desired_version), ("pending", 1, 2))
        await self.worker.run_once()
        self.assertEqual((await self.state()).applied_version, 2)
        self.assertEqual(self.gateway.deletes, 1)

    async def test_old_lease_cannot_apply_calendar_result(self):
        old = await self.worker.claim()
        result = await self.adapters.deliver(old)
        self.now += timedelta(seconds=91)
        new = await self.worker.claim()
        self.assertFalse(await self.worker.finish(old, result=result))
        self.assertEqual((await self.state()).applied_version, 0)
        await self.worker.finish(new, result=await self.adapters.deliver(new))
        self.assertEqual((await self.state()).state, "synced")

    async def test_conference_pending_then_ready_without_second_event(self):
        self.gateway.pending_conference = True
        await self.worker.run_once()
        self.assertEqual((await self.state()).applied_version, 1)
        self.assertEqual((await self.state()).state, "pending")
        self.gateway.events[self.event_id]["hangoutLink"] = "https://meet.google.com/abc-defg-hij"
        self.now += timedelta(seconds=5)
        await self.worker.run_once()
        self.assertEqual((await self.state()).state, "synced")
        self.assertEqual(self.gateway.inserts, 1)

    async def test_calendar_binding_change_stops_network(self):
        async with self.sessions() as session:
            profile = await session.scalar(select(OwnerProfile))
            profile.calendar_id = "other-calendar"
            await session.commit()
        await self.worker.run_once()
        self.assertEqual((await self.state()).state, "failed")
        self.assertEqual(self.gateway.inserts, 0)

    async def test_delay_notice_then_followup_link(self):
        self.gateway.pending_conference = True
        notice_id = await self.notice()
        await self.worker.run_once()
        self.now += timedelta(minutes=11)
        await self.worker.run_once()  # retry calendar, still pending
        await self.worker.run_once()  # delay notice
        self.assertIn("задерживаются", self.bot.send_message.await_args.args[1])
        self.assertNotIn("https://meet.google.com/", self.bot.send_message.await_args.args[1])
        self.gateway.events[self.event_id]["hangoutLink"] = "https://meet.google.com/abc-defg-hij"
        self.now += timedelta(minutes=1)
        await self.worker.run_once()
        await self.worker.run_once()
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertIn("https://meet.google.com/", self.bot.send_message.await_args.args[1])
        async with self.sessions() as session:
            notice = await session.get(OutboxEvent, notice_id)
            self.assertEqual(notice.error_code, "CALENDAR_DELAY_NOTICE")

    async def test_token_refresh_releases_write_lock_and_rechecks_credentials(self):
        self.settings.oauth_encryption_key = generate_encryption_key()
        key = self.settings.oauth_encryption_key
        async with self.sessions() as session:
            session.add(OAuthCredential(user_id=self.owner_id, status="active", provider="google",
                encrypted_access_token=encrypt_secret("old", key),
                encrypted_refresh_token=encrypt_secret("refresh", key), expires_at=self.now-timedelta(seconds=1)))
            await session.commit()
        async def refresh(settings, token):
            async def write(session):
                profile = await session.scalar(select(OwnerProfile))
                profile.version += 1
            await run_sqlite_write(self.sessions, write)
            return SimpleNamespace(access_token="new", expires_at=self.now+timedelta(hours=1))
        provider = GoogleTokens(self.sessions, settings=self.settings, owner_user_id=self.owner_id, clock=lambda: self.now)
        with patch("app.services.google_tokens.refresh_access_token", side_effect=refresh) as remote:
            self.assertEqual(await provider(), "new")
            self.assertEqual(await provider(), "new")
            self.assertEqual(remote.call_count, 1)

    async def test_reenqueue_same_synced_version_does_not_reset_state(self):
        await self.worker.run_once()
        async def enqueue(session):
            meeting = await session.get(Meeting, self.meeting_id)
            profile = await session.scalar(select(OwnerProfile))
            await enqueue_calendar_sync(session, meeting=meeting, profile=profile,
                                        environment="test", now=self.now)
        await run_sqlite_write(self.sessions, enqueue)
        self.assertEqual((await self.state()).state, "synced")
        self.assertFalse(await self.worker.run_once())

    async def test_calendar_finishes_while_delay_notice_in_flight(self):
        self.gateway.pending_conference = True
        await self.notice()
        await self.worker.run_once()
        self.now += timedelta(minutes=11)
        await self.worker.run_once()
        lease = await self.worker.claim()  # notification, Calendar retry not due yet
        notice_result = await self.notices.deliver(lease)  # remote delay message accepted
        self.assertEqual(notice_result.code, "CALENDAR_DELAY_NOTICE")
        self.gateway.events[self.event_id]["hangoutLink"] = "https://meet.google.com/abc-defg-hij"
        self.now += timedelta(seconds=30)
        await self.worker.run_once()  # Calendar finishes before notification ack
        await self.worker.finish(lease, result=notice_result)
        await self.worker.run_once()
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertIn("https://meet.google.com/", self.bot.send_message.await_args.args[1])

    async def test_etag_conflict_is_permanent_and_does_not_mark_new_version_synced(self):
        await self.worker.run_once()
        await self.change()
        self.gateway.patch = AsyncMock(side_effect=GoogleFailure(412))
        await self.worker.run_once()
        state = await self.state()
        self.assertEqual((state.state, state.applied_version, state.last_error_code),
                         ("failed", 1, "CALENDAR_EXTERNAL_EDIT"))

    async def test_environment_mismatch_never_calls_remote(self):
        adapters = CalendarDeliveries(self.sessions, gateway=self.gateway,
                                      owner_telegram_id=777, environment="production")
        await OutboxWorker(self.sessions, adapters.handlers(), clock=lambda: self.now).run_once()
        self.assertEqual(self.gateway.inserts, 0)
        self.assertEqual((await self.state()).applied_version, 0)


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_401_refreshes_once_and_429_is_safe(self):
        token = AsyncMock(side_effect=["old", "new"])
        requester = AsyncMock(side_effect=[(401, {}, {}), (429, {"error": {"message": "private"}}, {"Retry-After": "17"})])
        gateway = GoogleEventGateway(token, requester=requester)
        with self.assertRaises(GoogleFailure) as error:
            await gateway.get("calendar/id", "event/id")
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(error.exception.retry_after, 17)
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(token.await_args.kwargs, {"force_refresh": True})
        self.assertIn("calendar%2Fid", requester.await_args.args[1])

    async def test_delete_missing_and_etag(self):
        requester = AsyncMock(return_value=(410, {}, {}))
        gateway = GoogleEventGateway(AsyncMock(return_value="token"), requester=requester)
        await gateway.delete("calendar", "event", '"etag"')
        self.assertEqual(requester.await_args.kwargs["headers"]["If-Match"], '"etag"')

    async def test_quota_and_permission_errors_are_distinct(self):
        for reason, quota in (("rateLimitExceeded", True), ("forbidden", False)):
            requester = AsyncMock(return_value=(403, {"error": {"errors": [{"reason": reason}]}}, {}))
            gateway = GoogleEventGateway(AsyncMock(return_value="token"), requester=requester)
            with self.assertRaises(GoogleFailure) as error:
                await gateway.get("calendar", "event")
            self.assertEqual(error.exception.quota, quota)

    async def test_transient_token_failure_is_not_permanent_auth_failure(self):
        gateway = GoogleEventGateway(AsyncMock(side_effect=TimeoutError))
        with self.assertRaises(GoogleFailure) as error:
            await gateway.get("calendar", "event")
        self.assertEqual(error.exception.status, 0)
