"""Regression coverage on isolated SQLite; never reads application settings."""
import uuid
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import BookingRequest, Meeting, User
from app.db.repositories import get_busy_starts


class BookingAvailabilityRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_path = Path.cwd() / f".test_booking_{uuid.uuid4().hex}.db"
        path = self.db_path.as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.now = datetime.now(timezone.utc)
        self.start = self.now + timedelta(days=3)
        async with self.sessions() as session:
            client = User(telegram_id=101, full_name="Synthetic", role="client")
            session.add(client)
            await session.flush()
            self.client_id = client.id
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.db_path.unlink(missing_ok=True)

    async def seed(self, status="pending", reservation_minutes=30, meeting_status=None):
        async with self.sessions() as session:
            request = BookingRequest(
                client_id=self.client_id, title="Synthetic booking",
                start_at=self.start, end_at=self.start + timedelta(hours=1),
                status=status,
                reserved_until=(self.now + timedelta(minutes=reservation_minutes)
                                if reservation_minutes is not None else None),
            )
            session.add(request)
            await session.flush()
            if meeting_status:
                session.add(Meeting(
                    booking_request_id=request.id, client_id=self.client_id,
                    title=request.title, start_at=self.start + timedelta(days=1),
                    end_at=self.start + timedelta(days=1, hours=1), status=meeting_status,
                ))
            await session.commit()

    async def busy(self):
        async with self.sessions() as session:
            return await get_busy_starts(session)

    async def test_live_reservation_blocks_time(self):
        await self.seed()
        self.assertIn(self.start, await self.busy())

    async def test_expired_reservation_releases_time(self):
        await self.seed(reservation_minutes=-1)
        self.assertNotIn(self.start, await self.busy())

    async def test_null_reservation_does_not_block_time(self):
        await self.seed(reservation_minutes=None)
        self.assertNotIn(self.start, await self.busy())

    async def test_approved_request_does_not_block_old_time_after_move(self):
        await self.seed(status="approved", meeting_status="rescheduled")
        busy = await self.busy()
        self.assertNotIn(self.start, busy)
        self.assertIn(self.start + timedelta(days=1), busy)

    async def test_cancelled_meeting_and_approved_request_release_time(self):
        await self.seed(status="approved", meeting_status="cancelled_by_client")
        self.assertEqual(await self.busy(), set())


