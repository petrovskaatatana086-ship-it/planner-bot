"""Shared booking dependencies and an explicitly managed outbox lifecycle.

Construction does not access the network or start a worker. app.main starts
this runtime only with BOOKING_RUNTIME_ENABLED and an explicit environment.
"""
import asyncio
import math
import re

from sqlalchemy import select, inspect

from app.db.base import Base
from app.db.models import User
from app.integrations.google_events import GoogleEventGateway
from app.services.availability import GoogleAvailability
from app.services.availability_queries import AvailabilityQueries
from app.services.booking import BookingCommands
from app.services.bot_booking import BotBooking
from app.services.booking_deliveries import BookingDeliveries
from app.services.calendar_sync import CalendarDeliveries
from app.services.google_tokens import GoogleTokens
from app.services.outbox import OutboxWorker


async def verify_booking_schema(sessions):
    """create_all cannot upgrade old columns; refuse writers on a partial schema."""
    def verify(connection):
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        for name, table in Base.metadata.tables.items():
            if name not in tables or not set(table.columns.keys()).issubset(
                    {column["name"] for column in inspector.get_columns(name)}):
                raise RuntimeError("Booking schema is outdated; run Alembic upgrade head on a backed-up database")
    async with sessions() as session:
        connection = await session.connection()
        await connection.run_sync(verify)


class OwnerGoogleTokens:
    """Resolve the configured Telegram identity to its database ID lazily.

The owner may register after process startup. Never assume that either ID is 1
or cache an identity across deletion/blocking. The read ends before token refresh.
"""
    def __init__(self, sessions, *, settings, clock=None):
        self.sessions = sessions
        self.settings = settings
        self.clock = clock

    async def __call__(self, *, force_refresh=False):
        async with self.sessions() as session:
            owner_id = await session.scalar(select(User.id).where(
                User.telegram_id == self.settings.owner_telegram_id,
                User.status == "active"))
        if owner_id is None:
            raise ValueError("GOOGLE_AUTH_REQUIRED")
        tokens = GoogleTokens(self.sessions, settings=self.settings,
                              owner_user_id=owner_id, clock=self.clock)
        return await tokens(force_refresh=force_refresh)


class BookingRuntime:
    def __init__(self, *, commands, worker):
        self.commands = commands
        self.bot_commands = BotBooking(commands) if commands is not None else None
        self.availability_queries = AvailabilityQueries(commands) if commands is not None else None
        self.worker = worker
        self._task = None
        self._stop = None
        self._lifecycle = asyncio.Lock()

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    async def start(self):
        async with self._lifecycle:
            if self._task is not None:
                if self._task.done():
                    # Surface worker failures instead of silently restarting.
                    self._task.result()
                    raise RuntimeError("Booking worker stopped; close before restart")
                return
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self.worker.run(self._stop), name="booking-outbox")

    async def wait(self):
        """Observe failures without allowing a cancelled monitor to stop delivery."""
        if self._task is None:
            raise RuntimeError("Booking runtime is not started")
        await asyncio.shield(self._task)

    async def close(self, *, drain_seconds=35):
        if (isinstance(drain_seconds, bool) or not isinstance(drain_seconds, (int, float))
                or not math.isfinite(drain_seconds) or drain_seconds <= 0):
            raise ValueError("drain_seconds must be finite and positive")
        async with self._lifecycle:
            task = self._task
            if task is None:
                return
            self._stop.set()
            try:
                # Let the current delivery commit its acknowledgement before the
                # caller closes Bot/DB. A forced cancellation leaves its lease.
                done, _ = await asyncio.wait({task}, timeout=drain_seconds)
                if done:
                    await task
                else:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            finally:
                self._task = None
                self._stop = None


def build_booking_runtime(sessions, *, settings, bot, environment, clock=None, gateway=None):
    """Build one graph; environment is explicit and shared by writers/deliveries.

    Call close before closing the injected Bot or disposing sessions. Ownership
    of those resources remains with the caller. Tests may inject a fake gateway.
    """
    if not isinstance(environment, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", environment):
        raise ValueError("Invalid calendar environment")
    if gateway is None:
        gateway = GoogleEventGateway(OwnerGoogleTokens(sessions, settings=settings, clock=clock))
    commands = BookingCommands(sessions, owner_telegram_id=settings.owner_telegram_id,
        availability=GoogleAvailability(gateway, clock=clock),
        calendar_environment=environment, clock=clock)
    notices = BookingDeliveries(sessions, settings=settings, bot=bot,
        calendar_environment=environment, clock=clock)
    calendar = CalendarDeliveries(sessions, gateway=gateway,
        owner_telegram_id=settings.owner_telegram_id, environment=environment, clock=clock)
    worker = OutboxWorker(sessions, {**notices.handlers(), **calendar.handlers()}, clock=clock)
    return BookingRuntime(commands=commands, worker=worker)
