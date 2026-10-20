"""Google availability + booking transactions, with synthetic SQLite and fake APIs."""
import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.base import Base
from app.db.models import BookingRequest, CalendarSync, Meeting, OperationResult, OutboxEvent, OwnerProfile, User
from app.db.session import make_engine
from app.db.transactions import run_sqlite_write
from app.domain.intervals import Interval
from app.integrations.google_events import GoogleEventGateway, GoogleFailure
from app.services.availability import GoogleAvailability
from app.services.booking import BookingCommands, BookingError
from app.services.booking_deliveries import BookingDeliveries
from app.services.calendar_sync import CalendarDeliveries
from app.services.outbox import OutboxWorker
from test_calendar_sync import FakeCalendar


class BusyCalendar:
    def __init__(self):
        self.calls = []
        self.periods = ()
        self.on_query = None

    async def busy(self, calendar, start, end):
        self.calls.append((calendar, start, end))
        if self.on_query:
            await self.on_query()
        return self.periods


class GoogleBookingAvailabilityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path.cwd() / f".test_availability_{uuid.uuid4().hex}.db"
        self.settings = Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
            DATABASE_URL=f"sqlite+aiosqlite:///{self.path.as_posix()}")
        self.engines = [make_engine(self.settings), make_engine(self.settings)]
        self.sessions = [async_sessionmaker(e, expire_on_commit=False) for e in self.engines]
        async with self.engines[0].begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.now = datetime(2026, 10, 23, 7, tzinfo=timezone.utc)
        self.start = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
        async with self.sessions[0]() as session:
            owner = User(telegram_id=777, role="owner", full_name="Owner")
            clients = [User(telegram_id=x, role="client", full_name="Synthetic",
                            email=f"test{x}@example.com", consent_at=self.now) for x in (101, 102)]
            session.add_all([owner, *clients])
            await session.flush()
            self.owner_id = owner.id
            self.clients = [x.id for x in clients]
            session.add(OwnerProfile(user_id=owner.id, calendar_id="synthetic-calendar", google_status="active",
                                     default_duration=90, buffer_minutes=15))
            await session.commit()
        self.gateway = BusyCalendar()
        self.commands = [BookingCommands(s, owner_telegram_id=777, clock=lambda: self.now,
            availability=GoogleAvailability(self.gateway, clock=lambda: self.now), calendar_environment="test")
            for s in self.sessions]

    async def asyncTearDown(self):
        for engine in self.engines:
            await engine.dispose()
        self.path.unlink(missing_ok=True)

    def create(self, *, index=0, client=0, key=None, start=None):
        return self.commands[index].create_request(actor_id=self.clients[client],
            command_id=key or uuid.uuid4().hex, start_at=start or self.start,
            title="Synthetic booking", settings_version=1)

    async def approve(self, request, *, key=None):
        return await self.commands[0].approve_request(actor_id=self.owner_id,
            command_id=key or uuid.uuid4().hex, request_id=request["id"], expected_version=1)

    async def count(self, model):
        async with self.sessions[0]() as session:
            return await session.scalar(select(func.count()).select_from(model))

    async def test_create_approve_sync_and_notification_end_to_end(self):
        request = await self.create()
        result = await self.approve(request)
        self.assertEqual(await self.count(CalendarSync), 1)
        self.assertEqual(await self.count(OutboxEvent), 5)
        events = FakeCalendar()
        calendar = CalendarDeliveries(self.sessions[0], gateway=events, owner_telegram_id=777,
                                      environment="test", clock=lambda: self.now)
        bot = AsyncMock()
        notices = BookingDeliveries(self.sessions[0], settings=self.settings, bot=bot,
                                   calendar_environment="test", clock=lambda: self.now)
        worker = OutboxWorker(self.sessions[0], {**calendar.handlers(), **notices.handlers()}, clock=lambda: self.now)
        for _ in range(8):
            if not await worker.run_once():
                break
        async with self.sessions[0]() as session:
            sync = await session.get(CalendarSync, result["meeting_id"])
            self.assertEqual(sync.state, "synced")
        self.assertEqual(events.inserts, 1)
        bot.send_message.assert_awaited_once()
        self.assertIn("https://meet.google.com/", bot.send_message.await_args.args[1])

    async def test_busy_partial_overlap_is_rejected_without_writes(self):
        self.gateway.periods = (Interval(self.start+timedelta(minutes=30), self.start+timedelta(hours=2)),)
        with self.assertRaises(BookingError) as error:
            await self.create()
        self.assertEqual(error.exception.code, "SLOT_UNAVAILABLE")
        self.assertEqual(await self.count(BookingRequest), 0)
        self.assertEqual(await self.count(OperationResult), 0)
        self.assertEqual(await self.count(OutboxEvent), 0)

    async def test_external_buffer_and_query_coverage(self):
        self.gateway.periods = (Interval(self.start-timedelta(hours=1), self.start-timedelta(minutes=14)),)
        with self.assertRaises(BookingError) as error:
            await self.create()
        self.assertEqual(error.exception.code, "SLOT_UNAVAILABLE")
        _, begin, finish = self.gateway.calls[0]
        self.assertEqual(begin, self.start-timedelta(minutes=15))
        self.assertEqual(finish, self.start+timedelta(minutes=105))
        self.gateway.periods = (Interval(self.start-timedelta(hours=1), self.start-timedelta(minutes=15)),)
        await self.create()

    async def test_busy_appearing_before_approval_preserves_pending_request(self):
        request = await self.create()
        self.gateway.periods = (Interval(self.start, self.start+timedelta(hours=1)),)
        with self.assertRaises(BookingError) as error:
            await self.approve(request)
        self.assertEqual(error.exception.code, "SLOT_UNAVAILABLE")
        self.assertEqual(await self.count(Meeting), 0)
        self.assertEqual(await self.count(CalendarSync), 0)
        async with self.sessions[0]() as session:
            self.assertEqual((await session.get(BookingRequest, request["id"])).status, "pending")

    async def test_stale_snapshot_refetched_outside_transaction(self):
        async def first_slow_query():
            if len(self.gateway.calls) == 1:
                self.now += timedelta(seconds=6)
        self.gateway.on_query = first_slow_query
        await self.create()
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertEqual(await self.count(BookingRequest), 1)

    async def test_repeated_stale_snapshots_stop_after_two_passes(self):
        async def slow_query():
            self.now += timedelta(seconds=6)
        self.gateway.on_query = slow_query
        with self.assertRaises(BookingError) as error:
            await self.create()
        self.assertEqual(error.exception.code, "CALENDAR_UNAVAILABLE")
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertEqual(await self.count(OutboxEvent), 0)

    async def test_setting_change_without_legacy_version_bump_is_detected(self):
        async def change_buffer():
            if len(self.gateway.calls) == 1:
                async def mutate(session):
                    profile = await session.scalar(select(OwnerProfile))
                    profile.buffer_minutes = 20
                await run_sqlite_write(self.sessions[1], mutate)
        self.gateway.on_query = change_buffer
        await self.create()
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertEqual(self.gateway.calls[1][1], self.start-timedelta(minutes=20))

    async def test_binding_change_uses_only_new_calendar_snapshot(self):
        async def change_binding():
            if len(self.gateway.calls) == 1:
                async def mutate(session):
                    profile = await session.scalar(select(OwnerProfile))
                    profile.calendar_id = "new-calendar"
                await run_sqlite_write(self.sessions[1], mutate)
        self.gateway.on_query = change_binding
        await self.create()
        self.assertEqual([c[0] for c in self.gateway.calls], ["synthetic-calendar", "new-calendar"])

    async def test_external_query_does_not_hold_writer_lock(self):
        async def independent_write():
            async def mutate(session):
                client = await session.get(User, self.clients[1])
                client.full_name = "Changed independently"
            await asyncio.wait_for(run_sqlite_write(self.sessions[1], mutate), timeout=1)
        self.gateway.on_query = independent_write
        await self.create()

    async def test_local_competitor_during_external_query_is_rechecked(self):
        async def other_request():
            if len(self.gateway.calls) == 1:
                async def insert(session):
                    session.add(BookingRequest(client_id=self.clients[1], title="Competitor", status="pending",
                        start_at=self.start, end_at=self.start+timedelta(hours=1),
                        reserved_until=self.now+timedelta(minutes=30)))
                await run_sqlite_write(self.sessions[1], insert)
        self.gateway.on_query = other_request
        with self.assertRaises(BookingError) as error:
            await self.create()
        self.assertEqual(error.exception.code, "SLOT_UNAVAILABLE")
        self.assertEqual(await self.count(BookingRequest), 1)
        self.assertEqual(await self.count(OperationResult), 0)

    async def test_block_during_external_query_prevents_commit(self):
        async def block():
            async def update(session):
                client = await session.get(User, self.clients[0])
                client.status = "blocked"
            await run_sqlite_write(self.sessions[1], update)
        self.gateway.on_query = block
        with self.assertRaises(BookingError) as error:
            await self.create()
        self.assertEqual(error.exception.code, "ACCOUNT_BLOCKED")
        self.assertEqual(await self.count(BookingRequest), 0)

    async def test_invalid_local_schedule_does_not_query_google(self):
        with self.assertRaises(BookingError) as error:
            await self.create(start=self.start+timedelta(minutes=30))
        self.assertEqual(error.exception.code, "OUTSIDE_SCHEDULE")
        self.assertEqual(self.gateway.calls, [])

    async def test_two_connections_still_serialize_same_free_external_slot(self):
        results = await asyncio.gather(self.create(), self.create(index=1, client=1), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual([r.code for r in results if isinstance(r, BookingError)], ["SLOT_UNAVAILABLE"])
        self.assertEqual(await self.count(BookingRequest), 1)

    async def test_replay_does_not_depend_on_calendar_availability(self):
        key = uuid.uuid4().hex
        request = await self.create(key=key)
        async def unavailable():
            raise GoogleFailure(503)
        self.gateway.on_query = unavailable
        self.assertEqual(request, await self.create(index=1, key=key))
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_missing_provider_or_disconnected_calendar_never_assumed_free(self):
        command = BookingCommands(self.sessions[0], owner_telegram_id=777, clock=lambda: self.now,
                                  calendar_environment="test")
        with self.assertRaises(BookingError) as error:
            await command.create_request(actor_id=self.clients[0], command_id=uuid.uuid4().hex,
                start_at=self.start, title="Synthetic", settings_version=1)
        self.assertEqual(error.exception.code, "CALENDAR_UNAVAILABLE")
        async with self.sessions[0]() as session:
            profile = await session.scalar(select(OwnerProfile))
            profile.google_status = "error"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.create()
        self.assertEqual(error.exception.code, "CALENDAR_UNAVAILABLE")
        self.assertEqual(self.gateway.calls, [])

    async def test_snapshot_at_five_seconds_is_accepted(self):
        async def query():
            self.now += timedelta(seconds=5)
        self.gateway.on_query = query
        await self.create()
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_snapshot_after_five_seconds_is_not_accepted(self):
        async def query():
            self.now += timedelta(seconds=5, microseconds=1)
        self.gateway.on_query = query
        with self.assertRaises(BookingError) as error:
            await self.create()
        self.assertEqual(error.exception.code, "CALENDAR_UNAVAILABLE")
        self.assertEqual(await self.count(BookingRequest), 0)

    async def test_clock_moving_backwards_does_not_accept_future_snapshot(self):
        async def query():
            self.now -= timedelta(seconds=1)
        self.gateway.on_query = query
        with self.assertRaises(BookingError) as error:
            await self.create()
        self.assertEqual(error.exception.code, "CALENDAR_UNAVAILABLE")
        self.assertEqual(await self.count(BookingRequest), 0)

    async def test_approval_replay_succeeds_without_network(self):
        request = await self.create()
        key = uuid.uuid4().hex
        result = await self.approve(request, key=key)
        calls = len(self.gateway.calls)
        self.gateway.on_query = AsyncMock(side_effect=GoogleFailure(503))
        self.assertEqual(result, await self.approve(request, key=key))
        self.assertEqual(len(self.gateway.calls), calls)
        self.assertEqual(await self.count(Meeting), 1)
        self.assertEqual(await self.count(CalendarSync), 1)

    async def test_enqueue_failure_rolls_back_meeting_decision_and_delivery(self):
        request = await self.create()
        with patch("app.services.booking.enqueue_calendar_sync", side_effect=RuntimeError("synthetic")):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                await self.approve(request)
        self.assertEqual(await self.count(Meeting), 0)
        self.assertEqual(await self.count(CalendarSync), 0)
        self.assertEqual(await self.count(OperationResult), 1)
        self.assertEqual(await self.count(OutboxEvent), 2)
        async with self.sessions[0]() as session:
            self.assertEqual((await session.get(BookingRequest, request["id"])).status, "pending")

    async def test_replay_wins_if_other_connection_commits_while_query_fails(self):
        key = uuid.uuid4().hex
        committed = None
        async def competitor():
            nonlocal committed
            self.gateway.on_query = None
            committed = await self.create(index=1, key=key)
            raise GoogleFailure(503)
        self.gateway.on_query = competitor
        self.assertEqual(await self.create(key=key), committed)
        self.assertEqual(await self.count(BookingRequest), 1)
        self.assertEqual(await self.count(OutboxEvent), 2)


class FreeBusyGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_valid_busy_response_and_private_auth_header(self):
        begin = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
        finish = begin+timedelta(hours=2)
        response = {"timeMin": begin.isoformat(), "timeMax": finish.isoformat(),
            "calendars": {"calendar": {"busy": [{"start": begin.isoformat(), "end": finish.isoformat()}]}}}
        requester = AsyncMock(return_value=(200, response, {}))
        gateway = GoogleEventGateway(AsyncMock(return_value="synthetic-token"), requester=requester)
        intervals = await gateway.busy("calendar", begin, finish)
        self.assertEqual(intervals, (Interval(begin, finish),))
        self.assertEqual(requester.await_args.args[1], "https://www.googleapis.com/calendar/v3/freeBusy")
        self.assertEqual(requester.await_args.kwargs["headers"]["Authorization"], "Bearer synthetic-token")
        self.assertNotIn("sendUpdates", requester.await_args.kwargs["params"])

    async def test_http_200_calendar_error_missing_or_incomplete_response_fails_closed(self):
        begin = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
        finish = begin+timedelta(hours=2)
        bodies = [
            {},
            {"calendars": {"calendar": {"errors": [{"reason": "notFound"}], "busy": []}}},
            {"timeMin": begin.isoformat(), "timeMax": finish.isoformat(), "calendars": {}},
            {"timeMin": begin.isoformat(), "timeMax": finish.isoformat(), "calendars": {"calendar": {}}},
            {"timeMin": begin.isoformat(), "timeMax": finish.isoformat(), "calendars": {"calendar": {"busy": [], "errors": {}}}},
            {"timeMin": begin.isoformat(), "timeMax": begin.isoformat(), "calendars": {"calendar": {"busy": []}}},
            {"timeMin": begin.isoformat(), "timeMax": finish.isoformat(), "calendars": {"calendar": {"busy": [
                {"start": "2026-10-26T07:00:00", "end": "2026-10-26T08:00:00"}]}}},
            {"timeMin": begin.isoformat(), "timeMax": finish.isoformat(), "calendars": {"calendar": {"busy": [
                {"start": finish.isoformat(), "end": begin.isoformat()}]}}},
        ]
        for body in bodies:
            gateway = GoogleEventGateway(AsyncMock(return_value="token"), requester=AsyncMock(return_value=(200, body, {})))
            with self.subTest(body=body), self.assertRaises(GoogleFailure):
                await gateway.busy("calendar", begin, finish)
