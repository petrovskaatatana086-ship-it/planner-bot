"""Shared profiles + real bot adapter functions, isolated DB and fake Telegram."""
import asyncio
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot import handlers
from app.config import Settings
from app.db.base import Base
from app.db.models import AuditLog, OperationResult, OwnerProfile, User
from app.db.session import make_engine
from app.domain.registration import CONSENT_VERSION
from app.services.booking import BookingError
from app.services.profiles import ProfileCommands


class ProfileCommandsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path.cwd()/f".test_profiles_{uuid.uuid4().hex}.db"
        self.settings = Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
            DATABASE_URL=f"sqlite+aiosqlite:///{self.path.as_posix()}")
        self.engines = [make_engine(self.settings), make_engine(self.settings)]
        self.sessions = [async_sessionmaker(e, expire_on_commit=False) for e in self.engines]
        async with self.engines[0].begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.now = datetime(2026, 10, 21, 7, tzinfo=timezone.utc)
        self.commands = [ProfileCommands(s, owner_telegram_id=777, clock=lambda: self.now) for s in self.sessions]
        self.owner = await self.commands[0].ensure_telegram_user(telegram_id=777, full_name="Owner")
        self.client = await self.commands[0].ensure_telegram_user(telegram_id=101, full_name="Telegram name")

    async def asyncTearDown(self):
        for engine in self.engines:
            await engine.dispose()
        self.path.unlink(missing_ok=True)

    async def user(self):
        async with self.sessions[0]() as session:
            return await session.get(User, self.client.id)

    async def profile(self):
        async with self.sessions[0]() as session:
            return await session.scalar(select(OwnerProfile))

    def register(self, *, index=0, key=None, **override):
        data = dict(actor_id=self.client.id, command_id=key or uuid.uuid4().hex,
            full_name="Entered name", email="test@example.com", consent=True, consent_version=CONSENT_VERSION)
        data.update(override)
        return self.commands[index].register(**data)

    def message(self, *, owner=False, text=None, identifier=10):
        telegram_id = 777 if owner else 101
        return SimpleNamespace(from_user=SimpleNamespace(id=telegram_id, full_name="Telegram name", username="synthetic"),
            chat=SimpleNamespace(id=telegram_id), message_id=identifier, text=text, answer=AsyncMock())

    async def test_registration_evidence_and_replay(self):
        key = uuid.uuid4().hex
        result = await self.register(key=key)
        self.assertEqual(result, await self.register(index=1, key=key))
        user = await self.user()
        self.assertEqual(user.full_name, "Entered name")
        self.assertEqual(user.email, "test@example.com")
        self.assertEqual(user.consent_version, CONSENT_VERSION)
        self.assertFalse(result["registration_required"])
        async with self.sessions[0]() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(OperationResult)), 1)

    async def test_concurrent_registrations_do_not_overwrite_first_profile(self):
        results = await asyncio.gather(self.register(), self.register(index=1, full_name="Different name"),
                                       return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual([r.code for r in results if isinstance(r, BookingError)], ["INVALID_STATE"])
        self.assertIn((await self.user()).full_name, ("Entered name", "Different name"))

    async def test_concurrent_same_key_replays_registration(self):
        key = uuid.uuid4().hex
        first, second = await asyncio.gather(self.register(key=key), self.register(index=1, key=key))
        self.assertEqual(first, second)

    async def test_new_telegram_message_preserves_registered_name(self):
        await self.register()
        user = await self.commands[1].ensure_telegram_user(telegram_id=101, full_name="Changed Telegram name", username="new")
        self.assertEqual(user.full_name, "Entered name")
        self.assertEqual(user.username, "new")
        self.assertEqual(user.email, "test@example.com")

    async def test_ensure_user_race_has_one_stub(self):
        results = await asyncio.gather(*[self.commands[i].ensure_telegram_user(telegram_id=202, full_name="Synthetic")
                                        for i in (0, 1)])
        self.assertEqual(results[0].id, results[1].id)

    async def test_legitimate_emoji_display_name_is_not_rejected_as_control_text(self):
        user = await self.commands[0].ensure_telegram_user(telegram_id=303, full_name="Name 👩\u200d💻")
        self.assertEqual(user.telegram_id, 303)
        result = await self.register(full_name="Name 👩\u200d💻")
        self.assertEqual(result["full_name"], "Name 👩\u200d💻")

    async def test_blocked_registration_cannot_reactivate_account_or_replay(self):
        key = uuid.uuid4().hex
        await self.register(key=key)
        async with self.sessions[0]() as session:
            user = await session.get(User, self.client.id)
            user.status = "blocked"
            await session.commit()
        for kwargs in ({"key": key}, {}):
            with self.assertRaises(BookingError) as error:
                await self.register(**kwargs)
            self.assertEqual(error.exception.code, "ACCOUNT_BLOCKED")
        self.assertEqual((await self.user()).status, "blocked")

    async def test_blocked_unregistered_stub_cannot_register(self):
        async with self.sessions[0]() as session:
            user = await session.get(User, self.client.id)
            user.status = "blocked"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.register()
        self.assertEqual(error.exception.code, "ACCOUNT_BLOCKED")
        self.assertIsNone((await self.user()).email)
        self.assertIsNone((await self.user()).consent_at)

    async def test_consent_version_boolean_and_profile_validation(self):
        for data, code in (({"consent": False}, "VALIDATION_FAILED"),
                           ({"consent": 1}, "VALIDATION_FAILED"),
                           ({"consent_version": "outdated"}, "CONSENT_CHANGED"),
                           ({"full_name": "Bad\nName"}, "VALIDATION_FAILED"),
                           ({"full_name": " "}, "VALIDATION_FAILED"),
                           ({"email": "bad"}, "VALIDATION_FAILED")):
            with self.subTest(data=data), self.assertRaises(BookingError) as error:
                await self.register(**data)
            self.assertEqual(error.exception.code, code)
        self.assertIsNone((await self.user()).consent_at)

    async def test_existing_legacy_registration_not_forced_to_consent_again(self):
        async with self.sessions[0]() as session:
            user = await session.get(User, self.client.id)
            user.email = "legacy@example.com"
            user.consent_at = self.now
            user.full_name = "Legacy entered name"
            await session.commit()
        user = await self.commands[0].ensure_telegram_user(telegram_id=101, full_name="Telegram")
        self.assertIsNone(user.consent_version)
        self.assertEqual(user.full_name, "Legacy entered name")
        with self.assertRaises(BookingError) as error:
            await self.register()
        self.assertEqual(error.exception.code, "INVALID_STATE")

    async def test_patch_settings_version_and_same_key_replay(self):
        key = uuid.uuid4().hex
        data = dict(actor_id=self.owner.id, command_id=key, changes={"default_duration": 90}, expected_version=1)
        first = await self.commands[0].patch_settings(**data)
        self.assertEqual(first["version"], 2)
        self.assertEqual(first, await self.commands[1].patch_settings(**data))
        self.assertEqual((await self.profile()).default_duration, 90)
        with self.assertRaises(BookingError) as error:
            await self.commands[0].patch_settings(actor_id=self.owner.id, command_id=uuid.uuid4().hex,
                changes={"buffer_minutes": 15}, expected_version=1)
        self.assertEqual(error.exception.code, "STALE_VERSION")

    async def test_working_days_close_week_reopen_weekend_and_replay(self):
        from datetime import date
        from app.db.models import ScheduleRule
        from app.domain.booking_policy import schedule_windows
        data = dict(actor_id=self.owner.id, command_id=uuid.uuid4().hex,
                    changes={"working_days": []}, expected_version=1)
        result = await self.commands[0].patch_settings(**data)
        self.assertEqual(result["working_days"], [])
        self.assertEqual(result["version"], 2)
        self.assertEqual(result, await self.commands[1].patch_settings(**data))
        async with self.sessions[0]() as session:
            rules = (await session.scalars(select(ScheduleRule))).all()
            self.assertEqual(len(rules), 7)
            self.assertEqual(schedule_windows(rules, date(2026, 10, 26)), ())
        result = await self.commands[0].patch_settings(actor_id=self.owner.id,
            command_id=uuid.uuid4().hex, changes={"working_days": [6, 5]}, expected_version=2)
        self.assertEqual(result["working_days"], [5, 6])
        async with self.sessions[0]() as session:
            rules = (await session.scalars(select(ScheduleRule))).all()
            self.assertTrue(schedule_windows(rules, date(2026, 10, 31)))
            self.assertEqual(schedule_windows(rules, date(2026, 10, 26)), ())

    async def test_working_days_strict_validation(self):
        for days in ([True], [7], [-1], [1, 1], "1", None, [1.5]):
            with self.subTest(days=days), self.assertRaises(BookingError):
                await self.commands[0].patch_settings(actor_id=self.owner.id,
                    command_id=uuid.uuid4().hex, changes={"working_days": days}, expected_version=1)

    async def test_two_concurrent_card_updates_have_one_winner(self):
        results = await asyncio.gather(*[self.commands[i].patch_settings(actor_id=self.owner.id,
            command_id=uuid.uuid4().hex, changes={"daily_limit": 10+i}, expected_version=1)
            for i in (0, 1)], return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual([r.code for r in results if isinstance(r, BookingError)], ["STALE_VERSION"])
        self.assertEqual((await self.profile()).version, 2)

    async def test_absolute_bot_messages_serialize_without_losing_other_fields(self):
        await asyncio.gather(
            self.commands[0].apply_settings_message(actor_id=self.owner.id, command_id="message-1", changes={"default_duration": 90}),
            self.commands[1].apply_settings_message(actor_id=self.owner.id, command_id="message-2", changes={"buffer_minutes": 15}))
        profile = await self.profile()
        self.assertEqual((profile.default_duration, profile.buffer_minutes, profile.version), (90, 15, 3))

    async def test_strict_settings_types_ranges_and_no_unknown_fields(self):
        for changes in ({}, {"unknown": 1}, {"daily_limit": True}, {"default_duration": 14},
                        {"buffer_minutes": 121}, {"booking_enabled": 1}, {"daily_limit": 2.5}):
            with self.subTest(changes=changes), self.assertRaises(BookingError):
                await self.commands[0].patch_settings(actor_id=self.owner.id, command_id=uuid.uuid4().hex,
                    changes=changes, expected_version=1)
        self.assertEqual((await self.profile()).version, 1)

    async def test_stored_owner_role_cannot_grant_settings_access(self):
        async with self.sessions[0]() as session:
            client = await session.get(User, self.client.id)
            client.role = "owner"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.commands[0].patch_settings(actor_id=self.client.id, command_id=uuid.uuid4().hex,
                changes={"daily_limit": 12}, expected_version=1)
        self.assertEqual(error.exception.code, "FORBIDDEN")

    async def test_reused_key_different_payload_does_not_change_settings(self):
        await self.commands[0].apply_settings_message(actor_id=self.owner.id, command_id="message",
                                                    changes={"daily_limit": 10})
        with self.assertRaises(BookingError) as error:
            await self.commands[0].apply_settings_message(actor_id=self.owner.id, command_id="message",
                                                        changes={"daily_limit": 11})
        self.assertEqual(error.exception.code, "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual((await self.profile()).daily_limit, 10)

    async def test_real_bot_setting_handler_is_versioned_and_idempotent(self):
        message = self.message(owner=True, text="Длительность 90")
        await handlers.update_numeric_meeting_setting(message, self.settings, self.sessions[0])
        await handlers.update_numeric_meeting_setting(message, self.settings, self.sessions[1])
        self.assertEqual((await self.profile()).version, 2)
        self.assertEqual((await self.profile()).default_duration, 90)
        self.assertIn("90 мин.", message.answer.await_args.args[0])
        message.text = "Запись выкл"
        message.message_id += 1
        await handlers.toggle_booking(message, self.settings, self.sessions[0])
        self.assertFalse((await self.profile()).booking_enabled)
        self.assertEqual((await self.profile()).version, 3)

    async def test_real_bot_registration_uses_explicit_consent_and_preserves_name(self):
        message = self.message()
        state = AsyncMock()
        state.get_data.return_value = dict(full_name="Entered name", email="test@example.com",
                                          consent=True, consent_version=CONSENT_VERSION)
        await handlers.confirm_registration(message, state, self.settings, self.sessions[0])
        state.clear.assert_awaited_once()
        self.assertEqual((await self.user()).consent_version, CONSENT_VERSION)
        loaded = await handlers._load_user(message, self.settings, self.sessions[1])
        self.assertEqual(loaded.full_name, "Entered name")

    async def test_bot_registration_without_consent_does_not_write_profile(self):
        message = self.message()
        state = AsyncMock()
        state.get_data.return_value = dict(full_name="Entered name", email="test@example.com")
        await handlers.confirm_registration(message, state, self.settings, self.sessions[0])
        state.clear.assert_not_awaited()
        self.assertIsNone((await self.user()).email)
        self.assertNotIn("Регистрация завершена", message.answer.await_args.args[0])

    async def test_consent_button_records_shared_version(self):
        state = AsyncMock()
        await handlers.accept_consent(self.message(), state)
        state.update_data.assert_awaited_once_with(consent=True, consent_version=CONSENT_VERSION)

    def access(self, *, index=0, key="block", **overrides):
        data = dict(actor_id=self.owner.id, command_id=key, client_id=self.client.id, blocked=True)
        data.update(overrides)
        return self.commands[index].set_client_access(**data)

    async def test_access_replay_after_opposite_decision_preserves_profile(self):
        await self.register()
        before = await self.user()
        first = await self.access()
        await self.access(key="unblock", blocked=False)
        self.assertEqual(await self.access(index=1), first)
        after = await self.user()
        self.assertEqual(after.status, "active")
        for field in ("full_name", "email", "consent_at", "consent_version"):
            self.assertEqual(getattr(before, field), getattr(after, field))
        async with self.sessions[0]() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(AuditLog).where(
                AuditLog.action.like("client.status.%"))), 2)

    async def test_concurrent_access_same_key_has_one_audit(self):
        first, second = await asyncio.gather(self.access(), self.access(index=1))
        self.assertEqual(first, second)
        self.assertEqual((await self.user()).status, "blocked")
        async with self.sessions[0]() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(OperationResult)), 1)

    async def test_access_authorization_uses_telegram_identity_before_replay(self):
        async with self.sessions[0]() as session:
            (await session.get(User, self.client.id)).role = "owner"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.access(actor_id=self.client.id)
        self.assertEqual(error.exception.code, "FORBIDDEN")
        await self.access()
        async with self.sessions[0]() as session:
            (await session.get(User, self.owner.id)).status = "blocked"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.access()
        self.assertEqual(error.exception.code, "ACCOUNT_BLOCKED")

    async def test_access_rejects_owner_missing_invalid_and_reused_key(self):
        for overrides, code in ((dict(client_id=self.owner.id), "RESOURCE_NOT_FOUND"),
                                (dict(client_id=99999), "RESOURCE_NOT_FOUND"),
                                (dict(blocked=1), "VALIDATION_FAILED"),
                                (dict(client_id=True), "VALIDATION_FAILED")):
            with self.subTest(overrides=overrides), self.assertRaises(BookingError) as error:
                await self.access(**overrides)
            self.assertEqual(error.exception.code, code)
        await self.access()
        with self.assertRaises(BookingError) as error:
            await self.access(blocked=False)
        self.assertEqual(error.exception.code, "IDEMPOTENCY_KEY_REUSED")

    async def test_bot_access_retry_does_not_restore_old_block(self):
        message = self.message(owner=True, text=f"Заблокировать клиента {self.client.id}")
        await handlers.block_client(message, self.settings, self.sessions[0])
        self.assertEqual((await self.user()).status, "blocked")
        unblock = self.message(owner=True, text=f"Разблокировать клиента {self.client.id}", identifier=11)
        await handlers.unblock_client(unblock, self.settings, self.sessions[1])
        await handlers.block_client(message, self.settings, self.sessions[1])
        self.assertEqual((await self.user()).status, "active")
