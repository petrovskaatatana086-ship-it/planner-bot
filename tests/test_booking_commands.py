import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.base import Base
from app.db.models import BookingRequest, Meeting, OperationResult, OutboxEvent, OwnerProfile, User
from app.db.session import make_engine
from app.db.transactions import DatabaseBusy, run_sqlite_write
from app.domain.intervals import Interval
from app.services.booking import BookingCommands, BookingError


class IntervalTest(unittest.TestCase):
    def test_partial_overlap_gap_boundary_and_naive_utc(self):
        start = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
        a = Interval(start, start+timedelta(minutes=90))
        self.assertTrue(a.conflicts(Interval(start+timedelta(hours=1), start+timedelta(hours=2))))
        adjacent = Interval(a.end, a.end+timedelta(hours=1))
        self.assertFalse(a.conflicts(adjacent))
        self.assertTrue(a.conflicts(adjacent, 15))
        after_gap = Interval(a.end+timedelta(minutes=15), a.end+timedelta(hours=1))
        self.assertFalse(a.conflicts(after_gap, 15))
        self.assertTrue(a.conflicts(Interval(start.replace(tzinfo=None), a.end.replace(tzinfo=None))))
        with self.assertRaises(ValueError):
            Interval(start, start)


class BookingCommandsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path.cwd() / f".test_commands_{uuid.uuid4().hex}.db"
        self.settings = Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
                                 DATABASE_URL=f"sqlite+aiosqlite:///{self.path.as_posix()}")
        self.engines = [make_engine(self.settings), make_engine(self.settings)]
        self.sessions = [async_sessionmaker(e, expire_on_commit=False) for e in self.engines]
        async with self.engines[0].begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.now = datetime(2026, 10, 23, 7, tzinfo=timezone.utc)
        self.start = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)  # Monday 10 MSK
        async with self.sessions[0]() as session:
            owner = User(telegram_id=777, full_name="Owner", role="owner")
            clients = [User(telegram_id=n, full_name="Synthetic", role="client",
                            email=f"test{n}@example.com", consent_at=self.now) for n in (101, 102)]
            session.add_all([owner, *clients])
            await session.flush()
            self.owner_id = owner.id
            self.clients = [u.id for u in clients]
            profile = OwnerProfile(user_id=owner.id, default_duration=90, buffer_minutes=15)
            session.add(profile)
            await session.commit()
        self.commands = [BookingCommands(s, owner_telegram_id=777, clock=lambda: self.now)
                         for s in self.sessions]

    async def asyncTearDown(self):
        for engine in self.engines:
            await engine.dispose()
        self.path.unlink(missing_ok=True)

    def create(self, index=0, *, client=0, key=None, start=None, version=1):
        return self.commands[index].create_request(actor_id=self.clients[client],
            command_id=key or uuid.uuid4().hex, start_at=start or self.start,
            title="Consultation", settings_version=version)

    async def count(self, model):
        async with self.sessions[0]() as session:
            return await session.scalar(select(func.count()).select_from(model))

    async def approve(self, request, *, index=0, key=None):
        return await self.commands[index].approve_request(actor_id=self.owner_id,
            command_id=key or uuid.uuid4().hex, request_id=request["id"],
            expected_version=request["version"])

    async def test_two_engines_conflicting_intervals_have_one_winner(self):
        results = await asyncio.gather(self.create(),
            self.create(1, client=1, start=self.start+timedelta(hours=1)), return_exceptions=True)
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        errors = [x for x in results if isinstance(x, BookingError)]
        self.assertEqual([x.code for x in errors], ["SLOT_UNAVAILABLE"])
        self.assertEqual(await self.count(BookingRequest), 1)
        self.assertEqual(await self.count(OperationResult), 1)
        self.assertEqual(await self.count(OutboxEvent), 2)

    async def test_concurrent_same_key_replays_one_commit(self):
        key = uuid.uuid4().hex
        first, second = await asyncio.gather(self.create(key=key), self.create(1, key=key))
        self.assertEqual(first, second)
        self.assertEqual(await self.count(BookingRequest), 1)
        self.assertEqual(await self.count(OutboxEvent), 2)
        # A new service/connection still sees the durable result.
        self.assertEqual(first, await self.create(1, key=key))

    async def test_same_key_different_payload_rejected(self):
        key = uuid.uuid4().hex
        await self.create(key=key)
        with self.assertRaises(BookingError) as error:
            await self.create(key=key, start=self.start+timedelta(hours=3))
        self.assertEqual(error.exception.code, "IDEMPOTENCY_KEY_REUSED")

    async def test_concurrent_approval_creates_one_meeting(self):
        request = await self.create()
        results = await asyncio.gather(self.approve(request), self.approve(request, index=1),
                                       return_exceptions=True)
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        self.assertEqual([x.code for x in results if isinstance(x, BookingError)], ["STALE_VERSION"])
        self.assertEqual(await self.count(Meeting), 1)

    async def test_approval_replay_preserves_outbox_and_meeting(self):
        request = await self.create()
        key = uuid.uuid4().hex
        result = await self.approve(request, key=key)
        self.assertEqual(result, await self.approve(request, key=key, index=1))
        self.assertEqual(await self.count(Meeting), 1)
        self.assertEqual(await self.count(OutboxEvent), 4)

    async def test_expired_request_cannot_be_approved_over_new_reservation(self):
        request = await self.create()
        self.now += timedelta(minutes=31)
        await self.create(1, client=1)
        with self.assertRaises(BookingError) as error:
            await self.approve(request)
        self.assertEqual(error.exception.code, "SLOT_UNAVAILABLE")
        self.assertEqual(await self.count(Meeting), 0)
        async with self.sessions[0]() as session:
            original = await session.get(BookingRequest, request["id"])
            self.assertEqual(original.status, "pending")
            self.assertEqual(original.version, 1)

    async def test_buffer_and_daily_limit(self):
        await self.create()
        # First ends 11:30 MSK; 12:00 is permitted with 15 minute buffer.
        await self.create(client=1, start=self.start+timedelta(hours=2))
        async with self.sessions[0]() as session:
            profile = await session.scalar(select(OwnerProfile))
            profile.daily_limit = 2
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.create(start=self.start+timedelta(hours=4))
        self.assertEqual(error.exception.code, "DAILY_LIMIT_REACHED")

    async def test_disabled_connected_stale_and_unauthorized_fail_closed(self):
        with self.assertRaises(BookingError) as error:
            await self.create(version=2)
        self.assertEqual(error.exception.code, "STALE_VERSION")
        async with self.sessions[0]() as session:
            profile = await session.scalar(select(OwnerProfile))
            profile.google_status = "active"
            profile.calendar_id = "synthetic-calendar"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.create()
        self.assertEqual(error.exception.code, "CALENDAR_UNAVAILABLE")
        async with self.sessions[0]() as session:
            profile = await session.scalar(select(OwnerProfile))
            profile.booking_enabled = False
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.create()
        self.assertEqual(error.exception.code, "BOOKING_DISABLED")
        self.assertEqual(await self.count(OperationResult), 0)
        self.assertEqual(await self.count(OutboxEvent), 0)

    async def test_block_after_success_forbids_replay(self):
        key = uuid.uuid4().hex
        await self.create(key=key)
        async with self.sessions[0]() as session:
            actor = await session.get(User, self.clients[0])
            actor.status = "blocked"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.create(key=key)
        self.assertEqual(error.exception.code, "ACCOUNT_BLOCKED")

    async def test_failure_rolls_back_and_next_command_can_proceed(self):
        async def failing(session):
            profile = await session.scalar(select(OwnerProfile))
            profile.daily_limit = 1
            await session.flush()
            raise RuntimeError("synthetic fault")
        with self.assertRaisesRegex(RuntimeError, "synthetic fault"):
            await run_sqlite_write(self.sessions[0], failing)
        async with self.sessions[0]() as session:
            profile = await session.scalar(select(OwnerProfile))
            self.assertEqual(profile.daily_limit, 8)
        await self.create(1)

    async def test_busy_exhaustion_then_recovery(self):
        # Independent engine with short timeout makes an intentional held lock deterministic.
        short_engine = create_async_engine(self.settings.database_url, connect_args={"timeout": 0.01})
        try:
            short_sessions = async_sessionmaker(short_engine)
            async with self.sessions[0]() as blocker:
                await blocker.execute(text("BEGIN IMMEDIATE"))
                async def operation(session):
                    return 123
                with self.assertRaises(DatabaseBusy):
                    await run_sqlite_write(short_sessions, operation, retries=1)
                await blocker.rollback()
            self.assertEqual(await run_sqlite_write(short_sessions, operation), 123)
        finally:
            await short_engine.dispose()

    async def test_foreign_keys_enabled(self):
        async with self.sessions[0]() as session:
            self.assertEqual(await session.scalar(text("PRAGMA foreign_keys")), 1)
            self.assertEqual(await session.scalar(text("PRAGMA busy_timeout")), 5000)

    async def test_client_cannot_approve_even_if_stored_role_is_owner(self):
        request = await self.create()
        async with self.sessions[0]() as session:
            actor = await session.get(User, self.clients[0])
            actor.role = "owner"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.commands[1].approve_request(actor_id=self.clients[0],
                command_id=uuid.uuid4().hex, request_id=request["id"], expected_version=1)
        self.assertEqual(error.exception.code, "FORBIDDEN")
        self.assertEqual(await self.count(Meeting), 0)

    async def test_exact_reservation_expiry_releases_time(self):
        await self.create()
        self.now += timedelta(minutes=30)
        await self.create(1, client=1)
        self.assertEqual(await self.count(BookingRequest), 2)

    async def test_naive_time_and_off_grid_and_weekend_rejected(self):
        for start, code in (
            (self.start.replace(tzinfo=None), "VALIDATION_FAILED"),
            (self.start+timedelta(minutes=30), "OUTSIDE_SCHEDULE"),
            (self.start-timedelta(days=1), "OUTSIDE_SCHEDULE"),
        ):
            with self.subTest(start=start), self.assertRaises(BookingError) as error:
                await self.create(start=start)
            self.assertEqual(error.exception.code, code)
        self.assertEqual(await self.count(BookingRequest), 0)

    async def test_cancelled_task_releases_transaction(self):
        async def interrupted(session):
            profile = await session.scalar(select(OwnerProfile))
            profile.daily_limit = 1
            await session.flush()
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await run_sqlite_write(self.sessions[0], interrupted)
        await self.create(1)
        async with self.sessions[0]() as session:
            self.assertEqual((await session.scalar(select(OwnerProfile))).daily_limit, 8)
