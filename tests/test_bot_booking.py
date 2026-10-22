import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot import handlers
from app.bot.keyboards import owner_request_actions_keyboard
from app.config import Settings
from app.db.base import Base
from app.db.models import BotCommandInput, BookingRequest, CalendarSync, Meeting, OutboxEvent, OwnerProfile, RescheduleRequest, User
from app.db.session import make_engine
from app.services.booking import BookingError
from app.services.booking_runtime import build_booking_runtime
from app.services.bot_booking import BotBooking


class BotBookingTest(unittest.IsolatedAsyncioTestCase):
    def meeting_callback(self, data, *, owner=False, identifier="callback-meeting"):
        message = AsyncMock(spec=Message)
        message.answer = AsyncMock()
        return SimpleNamespace(data=data, id=identifier, from_user=self.message(owner=owner).from_user,
            message=message, answer=AsyncMock(), bot=self.bot)

    async def test_client_meeting_cards_show_actions_and_transfer_reaches_owner(self):
        meeting = await self.meeting()
        message = self.message()
        await handlers.my_meetings(message, self.settings, self.sessions, self.runtime)
        markup = message.answer.call_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"meeting_move:{meeting.id}:1")
        self.assertIn("Подтверждена", message.answer.call_args.args[0])
        callback = self.meeting_callback(markup.inline_keyboard[0][0].callback_data)
        await handlers.meeting_move_callback(callback, self.state, self.settings, self.sessions, self.runtime)
        await handlers.submit_meeting_move(self.message("27.10.2026 11:00"), self.state, self.settings, self.sessions, self.runtime)
        self.assertIsNone(await self.state.get_state())
        async with self.sessions() as session:
            current = await session.get(Meeting, meeting.id)
            self.assertEqual(current.status, "reschedule_pending")
            self.assertEqual(current.start_at, meeting.start_at)
            proposal = await session.scalar(select(RescheduleRequest))
        owner_message = self.message(owner=True)
        await handlers.pending_requests(owner_message, self.settings, self.sessions)
        decision = owner_message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertEqual(decision, f"ma:{meeting.id}:2:{proposal.id}")
        await handlers.owner_move_callback(self.meeting_callback(decision, owner=True), self.state,
                                           self.settings, self.sessions, self.runtime)
        async with self.sessions() as session:
            current = await session.get(Meeting, meeting.id)
            self.assertEqual(current.status, "rescheduled")
            self.assertEqual(current.start_at, datetime(2026, 10, 27, 8))

    async def test_cancel_button_requires_confirmation_and_replays_once(self):
        meeting = await self.meeting()
        callback = self.meeting_callback(f"meeting_cancel:{meeting.id}:1")
        await handlers.meeting_cancel_callback(callback, self.settings, self.sessions, self.runtime)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Meeting, meeting.id)).status, "confirmed")
        confirmation = callback.message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        yes = self.meeting_callback(confirmation)
        await handlers.confirm_meeting_cancel_callback(yes, self.settings, self.sessions, self.runtime)
        await handlers.confirm_meeting_cancel_callback(yes, self.settings, self.sessions, self.runtime)
        async with self.sessions() as session:
            current = await session.get(Meeting, meeting.id)
            self.assertEqual((current.status, current.version), ("cancelled_by_client", 2))

    async def test_foreign_and_stale_move_buttons_do_not_start_a_form(self):
        meeting = await self.meeting()
        async with self.sessions() as session:
            (await session.get(Meeting, meeting.id)).version = 2
            await session.commit()
        callback = self.meeting_callback(f"meeting_move:{meeting.id}:1")
        await handlers.meeting_move_callback(callback, self.state, self.settings, self.sessions, self.runtime)
        self.assertIsNone(await self.state.get_state())
        self.assertTrue(callback.answer.call_args.kwargs["show_alert"])
        async with self.sessions() as session:
            (await session.get(Meeting, meeting.id)).client_id = self.owner.id
            await session.commit()
        callback = self.meeting_callback(f"meeting_move:{meeting.id}:2")
        await handlers.meeting_move_callback(callback, self.state, self.settings, self.sessions, self.runtime)
        self.assertIsNone(await self.state.get_state())
        self.assertTrue(callback.answer.call_args.kwargs["show_alert"])

    async def test_move_form_validation_and_abandonment_preserve_meeting(self):
        meeting = await self.meeting()
        await handlers.meeting_move_callback(self.meeting_callback(f"meeting_move:{meeting.id}:1"), self.state,
                                             self.settings, self.sessions, self.runtime)
        await handlers.submit_meeting_move(self.message("wrong date"), self.state, self.settings, self.sessions, self.runtime)
        self.assertIsNotNone(await self.state.get_state())
        await handlers.submit_meeting_move(self.message("Отмена"), self.state, self.settings, self.sessions, self.runtime)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.count(RescheduleRequest), 0)

    async def test_owner_rejection_form_preserves_old_time_and_selected_proposal(self):
        meeting = await self.meeting()
        result = await self.adapter.execute("request_reschedule", actor_id=self.client.id, command_id="move-form-test",
            meeting_id=meeting.id, start_at=datetime(2026, 10, 27, 8, tzinfo=timezone.utc))
        callback = self.meeting_callback(f"mr:{meeting.id}:2:{result['reschedule_request_id']}", owner=True)
        await handlers.owner_move_callback(callback, self.state, self.settings, self.sessions, self.runtime)
        await handlers.reject_move_form(self.message("Другое время", owner=True), self.state,
                                        self.settings, self.sessions, self.runtime)
        async with self.sessions() as session:
            current = await session.get(Meeting, meeting.id)
            self.assertEqual(current.status, "confirmed")
            self.assertEqual(current.start_at, meeting.start_at)
            self.assertEqual((await session.get(RescheduleRequest, result['reschedule_request_id'])).status, "rejected")

    async def test_explicit_wrong_proposal_id_is_never_rebound_by_bot_adapter(self):
        meeting = await self.meeting()
        await self.adapter.execute("request_reschedule", actor_id=self.client.id, command_id="move-pin-test",
            meeting_id=meeting.id, start_at=datetime(2026, 10, 27, 8, tzinfo=timezone.utc))
        with self.assertRaises(BookingError):
            await self.adapter.execute("approve_reschedule", actor_id=self.owner.id, command_id="wrong-proposal",
                meeting_id=meeting.id, expected_version=2, reschedule_request_id=99999)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Meeting, meeting.id)).status, "reschedule_pending")

    async def test_owner_lists_and_moves_meeting_without_google(self):
        meeting = await self.meeting()
        message = self.message(owner=True)
        await handlers.my_meetings(message, self.settings, self.sessions, self.runtime)
        button = message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, f"owner_meeting_move:{meeting.id}:1")
        await handlers.meeting_move_callback(self.meeting_callback(button.callback_data, owner=True),
            self.state, self.settings, self.sessions, self.runtime)
        await handlers.submit_meeting_move(self.message("27.10.2026 11:00", owner=True),
            self.state, self.settings, self.sessions, self.runtime)
        self.assertIsNone(await self.state.get_state())
        async with self.sessions() as session:
            current = await session.get(Meeting, meeting.id)
            self.assertEqual(current.status, "rescheduled")
            self.assertEqual(current.start_at, datetime(2026, 10, 27, 8))

    async def test_owner_cancels_with_confirmation_and_replay(self):
        meeting = await self.meeting()
        callback = self.meeting_callback(f"owner_meeting_cancel:{meeting.id}:1", owner=True)
        await handlers.meeting_cancel_callback(callback, self.settings, self.sessions, self.runtime)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Meeting, meeting.id)).status, "confirmed")
        data = callback.message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        yes = self.meeting_callback(data, owner=True)
        await handlers.confirm_meeting_cancel_callback(yes, self.settings, self.sessions, self.runtime)
        await handlers.confirm_meeting_cancel_callback(yes, self.settings, self.sessions, self.runtime)
        async with self.sessions() as session:
            current = await session.get(Meeting, meeting.id)
            self.assertEqual((current.status, current.version), ("cancelled_by_owner", 2))

    async def test_client_cannot_use_owner_actions(self):
        meeting = await self.meeting()
        callback = self.meeting_callback(f"owner_meeting_move:{meeting.id}:1")
        await handlers.meeting_move_callback(callback, self.state, self.settings, self.sessions, self.runtime)
        self.assertIsNone(await self.state.get_state())
        self.assertTrue(callback.answer.call_args.kwargs["show_alert"])
        callback = self.meeting_callback(f"owner_meeting_cancel_yes:{meeting.id}:1")
        await handlers.confirm_meeting_cancel_callback(callback, self.settings, self.sessions, self.runtime)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Meeting, meeting.id)).status, "confirmed")

    async def test_owner_abandons_form_and_keeps_admin_menu(self):
        meeting = await self.meeting()
        await handlers.meeting_move_callback(self.meeting_callback(f"owner_meeting_move:{meeting.id}:1", owner=True),
            self.state, self.settings, self.sessions, self.runtime)
        message = self.message("Отмена", owner=True)
        await handlers.submit_meeting_move(message, self.state, self.settings, self.sessions, self.runtime)
        self.assertIsNone(await self.state.get_state())
        texts = [b.text for row in message.answer.call_args.kwargs["reply_markup"].keyboard for b in row]
        self.assertIn("Новые заявки", texts)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Meeting, meeting.id)).version, 1)

    async def test_owner_move_outside_schedule_explains_error_and_allows_retry(self):
        meeting = await self.meeting()
        await handlers.meeting_move_callback(self.meeting_callback(f"owner_meeting_move:{meeting.id}:1", owner=True),
            self.state, self.settings, self.sessions, self.runtime)
        message = self.message("01.11.2026 13:20", owner=True)
        await handlers.submit_meeting_move(message, self.state, self.settings, self.sessions, self.runtime)
        self.assertIn("не входит в расписание", message.answer.call_args.args[0])
        self.assertIsNotNone(await self.state.get_state())
        async with self.sessions() as session:
            current = await session.get(Meeting, meeting.id)
            self.assertEqual((current.start_at, current.version), (meeting.start_at, 1))
        await handlers.submit_meeting_move(self.message("27.10.2026 11:00", owner=True, identifier=11),
            self.state, self.settings, self.sessions, self.runtime)
        self.assertIsNone(await self.state.get_state())
        async with self.sessions() as session:
            self.assertEqual((await session.get(Meeting, meeting.id)).status, "rescheduled")

    async def asyncSetUp(self):
        self.path = Path.cwd()/f".test_bot_booking_{uuid.uuid4().hex}.db"
        self.settings = Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
            DATABASE_URL=f"sqlite+aiosqlite:///{self.path.as_posix()}")
        self.engine = make_engine(self.settings)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.now = datetime(2026, 10, 23, 7, tzinfo=timezone.utc)
        self.start = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
        async with self.sessions() as session:
            self.owner = User(telegram_id=777, full_name="Owner", role="owner")
            self.client = User(telegram_id=101, full_name="Client", role="client",
                               email="test@example.com", consent_at=self.now)
            session.add_all([self.owner, self.client])
            await session.flush()
            session.add(OwnerProfile(user_id=self.owner.id))
            await session.commit()
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.runtime = build_booking_runtime(self.sessions, settings=self.settings, bot=self.bot,
            environment="test", clock=lambda: self.now, gateway=SimpleNamespace())
        self.adapter = self.runtime.bot_commands
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=123, chat_id=101, user_id=101))

    async def asyncTearDown(self):
        await self.runtime.close()
        await self.engine.dispose()
        self.path.unlink(missing_ok=True)

    def message(self, text="", *, owner=False, identifier=10):
        actor = self.owner if owner else self.client
        return SimpleNamespace(text=text, message_id=identifier, bot=self.bot,
            from_user=SimpleNamespace(id=actor.telegram_id, full_name="Telegram name", username="synthetic"),
            chat=SimpleNamespace(id=actor.telegram_id), answer=AsyncMock())

    async def count(self, model):
        async with self.sessions() as session:
            return await session.scalar(select(func.count()).select_from(model))

    async def create(self):
        return await self.adapter.execute("create_request", actor_id=self.client.id,
            command_id=uuid.uuid4().hex, start_at=self.start, title="Consultation", settings_version=1)

    async def meeting(self):
        request = await self.create()
        result = await self.adapter.execute("approve_request", actor_id=self.owner.id,
            command_id=uuid.uuid4().hex, request_id=request["id"])
        async with self.sessions() as session:
            return await session.scalar(select(Meeting).where(Meeting.booking_request_id == result["id"]))

    async def test_approval_replays_after_commit_and_new_adapter(self):
        request = await self.create()
        key = uuid.uuid4().hex
        inputs = dict(actor_id=self.owner.id, command_id=key, request_id=request["id"])
        result = await self.adapter.execute("approve_request", **inputs)
        self.assertEqual(await BotBooking(self.runtime.commands).execute("approve_request", **inputs), result)
        self.assertEqual(await self.count(Meeting), 1)
        self.assertEqual(await self.count(OutboxEvent), 4)

    async def test_concurrent_same_message_captures_one_version(self):
        request = await self.create()
        key = uuid.uuid4().hex
        inputs = dict(actor_id=self.owner.id, command_id=key, request_id=request["id"])
        results = await asyncio.gather(*(self.adapter.execute("approve_request", **inputs) for _ in range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(await self.count(Meeting), 1)
        self.assertEqual(await self.count(BotCommandInput), 2)

    async def test_key_cannot_be_reused_for_another_action(self):
        request = await self.create()
        key = uuid.uuid4().hex
        await self.adapter.execute("approve_request", actor_id=self.owner.id,
                                   command_id=key, request_id=request["id"])
        with self.assertRaises(BookingError) as error:
            await self.adapter.execute("reject_request", actor_id=self.owner.id,
                command_id=key, request_id=request["id"], reason="Reason")
        self.assertEqual(error.exception.code, "IDEMPOTENCY_KEY_REUSED")

    async def test_cancel_replays_without_changing_version_or_notices(self):
        meeting = await self.meeting()
        message = self.message(f"Отменить мою встречу {meeting.id} Причина")
        await handlers.cancel_meeting_by_client(message, self.settings, self.sessions, self.runtime)
        count = await self.count(OutboxEvent)
        await handlers.cancel_meeting_by_client(message, self.settings, self.sessions, self.runtime)
        self.assertEqual(await self.count(OutboxEvent), count)
        async with self.sessions() as session:
            current = await session.get(Meeting, meeting.id)
            self.assertEqual(current.version, 2)
            self.assertEqual(current.status, "cancelled_by_client")
            self.assertEqual(current.cancellation_reason, "Причина")
        self.bot.send_message.assert_not_awaited()

    async def test_bot_submit_records_once_and_no_direct_send(self):
        data = dict(selected_start=self.start, title="Consultation", settings_version=1,
                    booking_command_id=uuid.uuid4().hex)
        await self.state.update_data(**data)
        await handlers.submit_booking(self.message(), self.state, self.settings, self.sessions, self.runtime)
        # Model a Telegram retry with the same preserved form snapshot.
        await self.state.update_data(**data)
        await handlers.submit_booking(self.message(identifier=11), self.state, self.settings, self.sessions, self.runtime)
        self.assertEqual(await self.count(BookingRequest), 1)
        self.assertEqual(await self.count(OutboxEvent), 2)
        self.bot.send_message.assert_not_awaited()

    async def test_stale_form_is_preserved_and_has_no_request(self):
        await self.state.update_data(selected_start=self.start, title="Consultation", settings_version=1,
            booking_command_id=uuid.uuid4().hex)
        async with self.sessions() as session:
            profile = await session.scalar(select(OwnerProfile))
            profile.version = 2
            await session.commit()
        message = self.message()
        await handlers.submit_booking(message, self.state, self.settings, self.sessions, self.runtime)
        self.assertEqual(await self.count(BookingRequest), 0)
        self.assertTrue(await self.state.get_data())
        self.assertIn("изменились", message.answer.call_args.args[0])

    async def test_no_runtime_does_not_fall_back_to_legacy_writer(self):
        await self.state.update_data(selected_start=self.start, title="Consultation", settings_version=1,
            booking_command_id=uuid.uuid4().hex)
        message = self.message()
        await handlers.submit_booking(message, self.state, self.settings, self.sessions)
        self.assertEqual(await self.count(BookingRequest), 0)
        self.assertIn("недоступны", message.answer.call_args.args[0])

    async def test_real_owner_approval_and_rejection_handlers(self):
        first = await self.create()
        message = self.message(f"Согласовать {first['id']}", owner=True)
        await handlers.approve_request(message, self.settings, self.sessions, self.runtime)
        await handlers.approve_request(message, self.settings, self.sessions, self.runtime)
        self.assertEqual(await self.count(Meeting), 1)
        # Another non-overlapping request to decline via actual FSM handlers.
        second = await self.adapter.execute("create_request", actor_id=self.client.id,
            command_id=uuid.uuid4().hex, start_at=self.start+timedelta(hours=2),
            title="Other consultation", settings_version=1)
        await handlers.start_reject_request(self.message(f"Отклонить {second['id']}", owner=True),
            self.state, self.settings, self.sessions)
        await handlers.reject_request_with_reason(self.message("Причина отказа", owner=True, identifier=12),
            self.state, self.settings, self.sessions, self.runtime)
        async with self.sessions() as session:
            self.assertEqual((await session.get(BookingRequest, second["id"])).status, "rejected")
        self.bot.send_message.assert_not_awaited()

    async def test_real_reschedule_handlers_and_specific_proposal_replay(self):
        meeting = await self.meeting()
        message = self.message(f"Запросить перенос {meeting.id} 27.10.2026 10:00 Комментарий")
        await handlers.request_reschedule_by_client(message, self.settings, self.sessions, self.runtime)
        owner_message = self.message(f"Согласовать перенос {meeting.id}", owner=True, identifier=20)
        await handlers.approve_client_reschedule(owner_message, self.settings, self.sessions, self.runtime)
        await handlers.approve_client_reschedule(owner_message, self.settings, self.sessions, self.runtime)
        self.assertEqual(await self.count(RescheduleRequest), 1)
        async with self.sessions() as session:
            current = await session.get(Meeting, meeting.id)
            self.assertEqual(current.status, "rescheduled")
            self.assertEqual(current.version, 3)
            self.assertEqual(current.start_at.date().isoformat(), "2026-10-27")
        self.bot.send_message.assert_not_awaited()

    async def test_real_rejection_owner_move_and_owner_cancel(self):
        meeting = await self.meeting()
        await handlers.request_reschedule_by_client(
            self.message(f"Запросить перенос {meeting.id} 27.10.2026 10:00"),
            self.settings, self.sessions, self.runtime)
        await handlers.reject_client_reschedule(
            self.message(f"Отклонить перенос {meeting.id} Причина", owner=True, identifier=20),
            self.settings, self.sessions, self.runtime)
        await handlers.reschedule_meeting_by_owner(
            self.message(f"Перенести {meeting.id} 28.10.2026 11:00", owner=True, identifier=21),
            self.settings, self.sessions, self.runtime)
        await handlers.cancel_meeting_by_owner(
            self.message(f"Отменить встречу {meeting.id} Отмена", owner=True, identifier=22),
            self.settings, self.sessions, self.runtime)
        async with self.sessions() as session:
            current = await session.get(Meeting, meeting.id)
            self.assertEqual(current.version, 5)
            self.assertEqual(current.status, "cancelled_by_owner")
        self.bot.send_message.assert_not_awaited()

    async def test_blocked_actor_cannot_replay(self):
        request = await self.create()
        key = uuid.uuid4().hex
        await self.adapter.execute("approve_request", actor_id=self.owner.id,
                                   command_id=key, request_id=request["id"])
        async with self.sessions() as session:
            (await session.get(User, self.owner.id)).status = "blocked"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.adapter.execute("approve_request", actor_id=self.owner.id,
                                       command_id=key, request_id=request["id"])
        self.assertEqual(error.exception.code, "ACCOUNT_BLOCKED")

    async def test_old_callback_fails_and_buttons_include_version(self):
        markup = owner_request_actions_keyboard(42, 7)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "approve_request:42:7")
        callback = SimpleNamespace(data="approve_request:42", id="fake",
            from_user=SimpleNamespace(id=777, full_name="Owner", username=None), answer=AsyncMock())
        await handlers.approve_request_callback(callback, self.settings, self.sessions, self.runtime)
        self.assertTrue(callback.answer.call_args.kwargs["show_alert"])

    async def test_start_form_captures_settings_and_new_form_identity_without_draft(self):
        await handlers.start_booking(self.message(), self.state, self.settings, self.sessions, self.runtime)
        first = await self.state.get_data()
        self.assertEqual(first["settings_version"], 1)
        self.assertTrue(first["booking_command_id"])
        await handlers.booking_change_date(self.message(), self.state, self.settings, self.sessions, self.runtime)
        self.assertNotEqual(first["booking_command_id"], (await self.state.get_data())["booking_command_id"])

    async def test_captured_proposal_decision_does_not_target_a_new_proposal(self):
        meeting = await self.meeting()
        await self.adapter.execute("request_reschedule", actor_id=self.client.id,
            command_id=uuid.uuid4().hex, meeting_id=meeting.id, start_at=self.start+timedelta(days=1))
        key = uuid.uuid4().hex
        inputs = dict(actor_id=self.owner.id, command_id=key, meeting_id=meeting.id, reason="Reason")
        result = await self.adapter.execute("reject_reschedule", **inputs)
        await self.adapter.execute("request_reschedule", actor_id=self.client.id,
            command_id=uuid.uuid4().hex, meeting_id=meeting.id, start_at=self.start+timedelta(days=2))
        self.assertEqual(result, await BotBooking(self.runtime.commands).execute("reject_reschedule", **inputs))
        async with self.sessions() as session:
            current = await session.get(Meeting, meeting.id)
            self.assertEqual(current.status, "reschedule_pending")
            self.assertEqual(current.version, 4)

    async def test_versioned_callback_does_not_adopt_current_version(self):
        request = await self.create()
        async with self.sessions() as session:
            (await session.get(BookingRequest, request["id"])).version = 2
            await session.commit()
        callback = SimpleNamespace(data=f"approve_request:{request['id']}:1", id="fake",
            bot=self.bot, message=None, from_user=SimpleNamespace(id=777, full_name="Owner", username=None),
            answer=AsyncMock())
        await handlers.approve_request_callback(callback, self.settings, self.sessions, self.runtime)
        self.assertEqual(await self.count(Meeting), 0)
        self.assertTrue(callback.answer.call_args.kwargs["show_alert"])


    async def test_failed_operation_does_not_rebind_versions_on_retry(self):
        request = await self.create()
        key = uuid.uuid4().hex
        inputs = dict(actor_id=self.owner.id, command_id=key, request_id=request["id"])
        with patch.object(self.runtime.commands, "approve_request", new_callable=AsyncMock,
                          side_effect=BookingError("CALENDAR_UNAVAILABLE")):
            with self.assertRaises(BookingError):
                await self.adapter.execute("approve_request", **inputs)
        async with self.sessions() as session:
            (await session.get(BookingRequest, request["id"])).version = 2
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await BotBooking(self.runtime.commands).execute("approve_request", **inputs)
        self.assertEqual(error.exception.code, "STALE_VERSION")
        self.assertEqual(await self.count(Meeting), 0)

    async def test_spoofed_owner_role_cannot_capture_approval(self):
        request = await self.create()
        async with self.sessions() as session:
            (await session.get(User, self.client.id)).role = "owner"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.adapter.execute("approve_request", actor_id=self.client.id,
                command_id=uuid.uuid4().hex, request_id=request["id"])
        self.assertEqual(error.exception.code, "FORBIDDEN")
        self.assertEqual(await self.count(BotCommandInput), 1)


    async def test_calendar_binding_error_is_safe_and_booking_is_rolled_back(self):
        meeting = await self.meeting()
        previous = await self.count(OutboxEvent)
        async with self.sessions() as session:
            session.add(CalendarSync(meeting_id=meeting.id, environment="other", calendar_id="test-calendar",
                deterministic_event_id="fake", conference_request_id="fake", desired_version=1,
                applied_version=1, state="synced"))
            await session.commit()
        message = self.message(f"Отменить мою встречу {meeting.id}")
        await handlers.cancel_meeting_by_client(message, self.settings, self.sessions, self.runtime)
        self.assertIn("Повторите позже", message.answer.call_args.args[0])
        async with self.sessions() as session:
            self.assertEqual((await session.get(Meeting, meeting.id)).status, "confirmed")
        self.assertEqual(await self.count(OutboxEvent), previous)


if __name__ == "__main__":
    unittest.main()
