"""Preview/command consistency, real SQLite and exclusively fake networks."""
import unittest
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot import handlers
from app.bot.states import Booking
from app.config import Settings
from app.db.base import Base
from app.db.models import BookingRequest, Meeting, OutboxEvent, OwnerProfile, ScheduleRule, User
from app.db.session import make_engine
from app.domain.booking import MOSCOW_TZ
from app.domain.intervals import Interval
from app.integrations.google_events import GoogleFailure
from app.services.booking import BookingError
from app.services.booking_runtime import build_booking_runtime


class AvailabilityQueriesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path.cwd()/f".test_preview_{uuid.uuid4().hex}.db"
        self.settings = Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
            DATABASE_URL=f"sqlite+aiosqlite:///{self.path.as_posix()}")
        self.engines = [make_engine(self.settings), make_engine(self.settings)]
        self.sessions = [async_sessionmaker(e, expire_on_commit=False) for e in self.engines]
        async with self.engines[0].begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.now = datetime(2026, 10, 23, 7, tzinfo=timezone.utc)
        self.start = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
        async with self.sessions[0]() as session:
            self.owner = User(telegram_id=777, full_name="Owner", role="owner")
            self.client = User(telegram_id=101, full_name="Client", role="client",
                email="test@example.com", consent_at=self.now)
            session.add_all([self.owner, self.client])
            await session.flush()
            session.add(OwnerProfile(user_id=self.owner.id))
            await session.commit()
        self.gateway = SimpleNamespace(busy=AsyncMock(return_value=[]), busy_excluding=AsyncMock(return_value=[]))
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.runtime = build_booking_runtime(self.sessions[0], settings=self.settings, bot=self.bot,
            environment="test", clock=lambda:self.now, gateway=self.gateway)
        self.queries = self.runtime.availability_queries
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=123, chat_id=101, user_id=101))

    async def asyncTearDown(self):
        await self.runtime.close()
        for engine in self.engines:
            await engine.dispose()
        self.path.unlink(missing_ok=True)

    async def profile(self, **values):
        async with self.sessions[1]() as session:
            profile = await session.scalar(select(OwnerProfile))
            for name, value in values.items():
                setattr(profile, name, value)
            await session.commit()

    async def slots(self, **values):
        data = dict(actor_id=self.client.id, date=self.start.date())
        data.update(values)
        return await self.queries.slots(**data)

    async def seed(self, *, start=None, duration=60, status="pending", reserve=30, meeting_status=None, client_id=None):
        start = start or self.start
        async with self.sessions[1]() as session:
            request = BookingRequest(client_id=client_id or self.client.id, title="Synthetic",
                start_at=start, end_at=start+timedelta(minutes=duration), status=status,
                reserved_until=self.now+timedelta(minutes=reserve) if reserve is not None else None)
            session.add(request)
            await session.flush()
            meeting = None
            if meeting_status:
                meeting = Meeting(booking_request_id=request.id, client_id=request.client_id, title=request.title,
                    start_at=start, end_at=request.end_at, status=meeting_status)
                session.add(meeting)
                await session.flush()
            await session.commit()
            return meeting or request

    def message(self, text=""):
        return SimpleNamespace(text=text, message_id=10, bot=self.bot,
            from_user=SimpleNamespace(id=101, full_name="Client", username=None),
            chat=SimpleNamespace(id=101), answer=AsyncMock())

    async def test_preview_creates_no_reserve_and_command_accepts_last_shown_start(self):
        await self.profile(default_duration=90)
        view = await self.slots()
        self.assertEqual(len(view.slots), 7)
        self.assertEqual(view.slots[0], Interval(self.start, self.start+timedelta(minutes=90)))
        async with self.sessions[0]() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(BookingRequest)), 0)
            self.assertEqual(await session.scalar(select(func.count()).select_from(OutboxEvent)), 0)
        # Verify the last slot against real command rules, including window end.
        result = await self.runtime.commands.create_request(actor_id=self.client.id, command_id=uuid.uuid4().hex,
            start_at=view.slots[-1].start, title="Consultation", settings_version=view.settings_version)
        self.assertEqual(result["status"], "pending")
        self.gateway.busy.assert_not_awaited()

    async def test_full_interval_overlap_and_single_buffer_boundary(self):
        await self.profile(buffer_minutes=15)
        await self.seed(start=self.start+timedelta(minutes=30), duration=45)
        starts = [slot.start for slot in (await self.slots()).slots]
        self.assertNotIn(self.start, starts)
        self.assertNotIn(self.start+timedelta(hours=1), starts)
        self.assertIn(self.start+timedelta(hours=2), starts)
        # Ends at 10:45 MSK, so 11:00 is allowed with one 15 minute buffer.
        async with self.sessions[1]() as session:
            request = await session.scalar(select(BookingRequest))
            request.end_at = self.start+timedelta(minutes=45)
            await session.commit()
        self.assertIn(self.start+timedelta(hours=1), [slot.start for slot in (await self.slots()).slots])

    async def test_expired_null_and_approved_reserves_do_not_block(self):
        await self.seed(reserve=0)
        await self.seed(reserve=None)
        await self.seed(status="approved")
        self.assertIn(self.start, [slot.start for slot in (await self.slots()).slots])

    async def test_daily_limit_counts_active_meeting_and_live_reserve(self):
        await self.profile(daily_limit=2)
        await self.seed(start=self.start, status="approved", meeting_status="reschedule_pending")
        await self.seed(start=self.start+timedelta(hours=4))
        self.assertEqual((await self.slots()).slots, ())

    async def test_custom_weekend_schedule_and_merged_hourly_grid(self):
        day = self.start.date()+timedelta(days=5)  # Saturday
        async with self.sessions[1]() as session:
            session.add_all([ScheduleRule(owner_id=self.owner.id, weekday=5, start_time=time(9,30), end_time=time(12)),
                ScheduleRule(owner_id=self.owner.id, weekday=5, start_time=time(11), end_time=time(14))])
            await session.commit()
        view = await self.slots(date=day)
        self.assertEqual([slot.start.astimezone(MOSCOW_TZ).strftime("%H:%M") for slot in view.slots],
                         ["09:30", "10:30", "11:30", "12:30"])
        self.assertEqual((await self.slots()).slots, ())  # no Monday rules
        await self.runtime.commands.create_request(actor_id=self.client.id, command_id=uuid.uuid4().hex,
            start_at=view.slots[1].start, title="Consultation", settings_version=1)

    async def test_inactive_only_schedule_is_closed_not_default(self):
        async with self.sessions[1]() as session:
            session.add(ScheduleRule(owner_id=self.owner.id, weekday=0, start_time=time(10), end_time=time(18), is_active=False))
            await session.commit()
        self.assertEqual((await self.slots()).slots, ())

    async def test_notice_and_horizon_boundaries(self):
        self.now = self.start-timedelta(hours=24)
        self.assertEqual((await self.slots()).slots[0].start, self.start)
        self.now += timedelta(microseconds=1)
        self.assertNotIn(self.start, [slot.start for slot in (await self.slots()).slots])
        await self.profile(booking_horizon_days=1)
        self.assertEqual((await self.slots(date=self.start.date()+timedelta(days=3))).slots, ())

    async def test_google_partial_overlap_buffer_and_one_bounded_query(self):
        await self.profile(calendar_id="test-calendar", google_status="active", buffer_minutes=15)
        self.gateway.busy.return_value = [Interval(self.start+timedelta(minutes=30), self.start+timedelta(minutes=45))]
        view = await self.slots()
        self.assertNotIn(self.start, [slot.start for slot in view.slots])
        self.assertIn(self.start+timedelta(hours=1), [slot.start for slot in view.slots])
        self.gateway.busy.assert_awaited_once()
        args = self.gateway.busy.call_args.args
        self.assertEqual(args[1], datetime(2026,10,25,20,45,tzinfo=timezone.utc))
        self.assertEqual(args[2]-args[1], timedelta(days=1, minutes=30))

    async def test_google_failure_is_safe_and_not_empty_success(self):
        await self.profile(calendar_id="test-calendar", google_status="active")
        self.gateway.busy.side_effect = GoogleFailure(503)
        with self.assertRaises(BookingError) as error:
            await self.slots()
        self.assertEqual(error.exception.code, "CALENDAR_UNAVAILABLE")

    async def test_no_database_lock_during_network_and_new_reserve_is_seen(self):
        await self.profile(calendar_id="test-calendar", google_status="active")
        async def busy(*args):
            await self.seed()  # independent engine must be able to write
            return []
        self.gateway.busy.side_effect = busy
        view = await self.slots()
        self.assertNotIn(self.start, [slot.start for slot in view.slots])

    async def test_profile_change_without_version_increment_repeats_fetch(self):
        await self.profile(calendar_id="test-calendar", google_status="active")
        async def busy(*args):
            if self.gateway.busy.await_count == 1:
                await self.profile(default_duration=90)
            return []
        self.gateway.busy.side_effect = busy
        view = await self.slots()
        self.assertEqual(view.slots[0].end-view.slots[0].start, timedelta(minutes=90))
        self.assertEqual(self.gateway.busy.await_count, 2)

    async def test_schedule_changed_during_fetch_is_rechecked(self):
        await self.profile(calendar_id="test-calendar", google_status="active")
        async def busy(*args):
            async with self.sessions[1]() as session:
                session.add(ScheduleRule(owner_id=self.owner.id, weekday=0,
                    start_time=time(13), end_time=time(15)))
                await session.commit()
            return []
        self.gateway.busy.side_effect = busy
        # Changing on every fetch is deliberately unstable: bounded fail-closed.
        with self.assertRaises(BookingError) as error:
            await self.slots()
        self.assertEqual(error.exception.code, "CALENDAR_UNAVAILABLE")
        self.assertEqual(self.gateway.busy.await_count, 2)

    async def test_snapshot_older_than_five_seconds_fails_closed(self):
        await self.profile(calendar_id="test-calendar", google_status="active")
        async def busy(*args):
            self.now += timedelta(seconds=6)
            return []
        self.gateway.busy.side_effect = busy
        with self.assertRaises(BookingError) as error:
            await self.slots()
        self.assertEqual(error.exception.code, "CALENDAR_UNAVAILABLE")

    async def test_blocked_during_network_is_not_returned_slots(self):
        await self.profile(calendar_id="test-calendar", google_status="active")
        async def busy(*args):
            async with self.sessions[1]() as session:
                (await session.get(User, self.client.id)).status = "blocked"
                await session.commit()
            return []
        self.gateway.busy.side_effect = busy
        with self.assertRaises(BookingError) as error:
            await self.slots()
        self.assertEqual(error.exception.code, "ACCOUNT_BLOCKED")

    async def test_meeting_ownership_checked_before_google_and_duration_preserved(self):
        meeting = await self.seed(status="approved", meeting_status="confirmed", duration=90)
        await self.profile(calendar_id="test-calendar", google_status="active", default_duration=30)
        async with self.sessions[1]() as session:
            (await session.get(Meeting, meeting.id)).google_event_id = "own-event"
            await session.commit()
        view = await self.slots(meeting_id=meeting.id)
        self.assertEqual(view.slots[0].start, self.start+timedelta(hours=1))
        self.assertEqual(view.slots[0].end-view.slots[0].start, timedelta(minutes=90))
        self.assertEqual(self.gateway.busy_excluding.call_args.args[-1], "own-event")
        async with self.sessions[1]() as session:
            stranger = User(telegram_id=102, full_name="Other", role="client", email="other@example.com", consent_at=self.now)
            session.add(stranger)
            await session.commit()
        self.gateway.busy_excluding.reset_mock()
        with self.assertRaises(BookingError) as error:
            await self.slots(actor_id=stranger.id, meeting_id=meeting.id)
        self.assertEqual(error.exception.code, "RESOURCE_NOT_FOUND")
        self.gateway.busy_excluding.assert_not_awaited()

    async def test_bounded_dates_and_invalid_inputs_do_not_call_google(self):
        for first, last in [(self.start.date(), self.start.date()+timedelta(days=31)),
                            ("2026-10-26", None), (self.start, None)]:
            with self.assertRaises(BookingError):
                await self.queries.days(actor_id=self.client.id, from_date=first, to_date=last)
        self.gateway.busy.assert_not_awaited()

    async def test_bot_pages_past_month_and_back_without_resetting_settings(self):
        await self.profile(booking_horizon_days=90)
        await handlers.start_booking(self.message(), self.state, self.settings, self.sessions[0], self.runtime)
        first = await self.state.get_data()
        self.assertTrue(first["date_next"])
        await handlers.choose_date(self.message("Следующие даты"), self.state, self.settings, self.sessions[0], self.runtime)
        second = await self.state.get_data()
        self.assertEqual(second["page_from"], first["page_to"]+timedelta(days=1))
        await handlers.choose_date(self.message("Предыдущие даты"), self.state, self.settings, self.sessions[0], self.runtime)
        self.assertEqual((await self.state.get_data())["page_from"], first["page_from"])

    async def test_real_bot_flow_uses_current_preview_and_stale_settings_fail(self):
        await handlers.start_booking(self.message(), self.state, self.settings, self.sessions[0], self.runtime)
        await handlers.choose_date(self.message("26.10.2026"), self.state, self.settings, self.sessions[0], self.runtime)
        self.assertEqual(await self.state.get_state(), Booking.waiting_for_time.state)
        await self.profile(version=2)
        message = self.message("10:00")
        await handlers.choose_time(message, self.state, self.settings, self.sessions[0], self.runtime)
        self.assertIn("изменились", message.answer.call_args.args[0])
        self.assertEqual(await self.state.get_state(), Booking.waiting_for_time.state)

    async def test_pending_client_proposal_is_checked_before_google(self):
        meeting = await self.seed(status="approved", meeting_status="reschedule_pending")
        await self.profile(calendar_id="test-calendar", google_status="active")
        with self.assertRaises(BookingError) as error:
            await self.slots(meeting_id=meeting.id)
        self.assertEqual(error.exception.code, "INVALID_STATE")
        self.gateway.busy.assert_not_awaited()
        self.gateway.busy_excluding.assert_not_awaited()

    async def test_empty_first_page_can_navigate_to_next_month(self):
        await self.profile(booking_horizon_days=90, calendar_id="test-calendar", google_status="active")
        first = self.now.astimezone(MOSCOW_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        self.gateway.busy.return_value = [Interval(first, first+timedelta(days=31))]
        await handlers.start_booking(self.message(), self.state, self.settings, self.sessions[0], self.runtime)
        data = await self.state.get_data()
        self.assertEqual(data["allowed_dates"], [])
        self.assertTrue(data["date_next"])
        self.assertEqual(self.gateway.busy.await_count, 1)
        args = self.gateway.busy.call_args.args
        self.assertEqual(args[2]-args[1], timedelta(days=31))
        await handlers.choose_date(self.message("Следующие даты"), self.state, self.settings, self.sessions[0], self.runtime)
        self.assertTrue((await self.state.get_data())["allowed_dates"])

    async def test_bot_rechecks_slot_and_restarts_after_a_reservation(self):
        await handlers.start_booking(self.message(), self.state, self.settings, self.sessions[0], self.runtime)
        await handlers.choose_date(self.message("26.10.2026"), self.state, self.settings, self.sessions[0], self.runtime)
        await self.seed()
        await handlers.choose_time(self.message("10:00"), self.state, self.settings, self.sessions[0], self.runtime)
        self.assertEqual(await self.state.get_state(), Booking.waiting_for_date.state)
        self.assertNotIn("selected_start", await self.state.get_data())

    async def test_safe_bot_error_does_not_change_form_or_expose_network_details(self):
        await self.profile(calendar_id="test-calendar", google_status="active")
        self.gateway.busy.side_effect = GoogleFailure(503)
        message = self.message()
        await handlers.start_booking(message, self.state, self.settings, self.sessions[0], self.runtime)
        self.assertIn("Google Calendar", message.answer.call_args.args[0])
        self.assertNotIn("503", message.answer.call_args.args[0])
        self.assertEqual(await self.state.get_data(), {})


if __name__ == "__main__":
    unittest.main()
