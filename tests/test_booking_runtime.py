"""Runtime graph and shutdown checks with isolated SQLite and fake transports."""
import asyncio
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.base import Base
from app.db.models import OutboxEvent, OwnerProfile, User
from app.db.session import make_engine
from app.services.booking_runtime import BookingRuntime, OwnerGoogleTokens, build_booking_runtime


class RuntimeLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_start_and_close_run_one_worker_and_allow_restart(self):
        entered = asyncio.Event()
        async def run(stop):
            entered.set()
            await stop.wait()
        worker = SimpleNamespace(run=AsyncMock(side_effect=run))
        runtime = BookingRuntime(commands=None, worker=worker)
        await asyncio.gather(runtime.start(), runtime.start())
        await entered.wait()
        self.assertTrue(runtime.running)
        await asyncio.gather(runtime.close(), runtime.close())
        self.assertFalse(runtime.running)
        self.assertEqual(worker.run.await_count, 1)
        await runtime.start()
        await runtime.close()
        self.assertEqual(worker.run.await_count, 2)

    async def test_shutdown_drains_current_delivery(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def run(stop):
            entered.set()
            await release.wait()
            self.assertTrue(stop.is_set())
            finished.set()
        runtime = BookingRuntime(commands=None, worker=SimpleNamespace(run=run))
        await runtime.start()
        await entered.wait()
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await closing
        self.assertTrue(finished.is_set())

    async def test_drain_timeout_cancels_and_joins_worker(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def run(stop):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        runtime = BookingRuntime(commands=None, worker=SimpleNamespace(run=run))
        await runtime.start()
        await entered.wait()
        await runtime.close(drain_seconds=0.01)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(runtime.running)

    async def test_cancelled_shutdown_joins_worker(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def run(stop):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        runtime = BookingRuntime(commands=None, worker=SimpleNamespace(run=run))
        await runtime.start()
        await entered.wait()
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.assertTrue(cancelled.is_set())
        self.assertFalse(runtime.running)

    async def test_worker_failure_is_visible_and_not_silently_restarted(self):
        async def run(stop):
            raise RuntimeError("test failure")
        runtime = BookingRuntime(commands=None, worker=SimpleNamespace(run=run))
        await runtime.start()
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, "test failure"):
            await runtime.start()
        with self.assertRaisesRegex(RuntimeError, "test failure"):
            await runtime.close()
        self.assertFalse(runtime.running)

    async def test_invalid_drain_timeout(self):
        runtime = BookingRuntime(commands=None, worker=None)
        for value in (0, -1, float("inf"), float("nan"), True, "1"):
            with self.assertRaises(ValueError):
                await runtime.close(drain_seconds=value)

    async def test_worker_timeout_error_is_not_confused_with_drain_timeout(self):
        async def run(stop):
            raise TimeoutError("worker failure")
        runtime = BookingRuntime(commands=None, worker=SimpleNamespace(run=run))
        await runtime.start()
        with self.assertRaisesRegex(TimeoutError, "worker failure"):
            await runtime.close()


class RuntimeGraphTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path.cwd()/f".test_runtime_{uuid.uuid4().hex}.db"
        self.settings = Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
            DATABASE_URL=f"sqlite+aiosqlite:///{self.path.as_posix()}")
        self.engine = make_engine(self.settings)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.now = datetime(2026, 10, 23, 7, tzinfo=timezone.utc)
        async with self.sessions() as session:
            client = User(telegram_id=101, full_name="Client", role="client",
                          email="test@example.com", consent_at=self.now)
            session.add(client)
            await session.flush()
            owner = User(telegram_id=777, full_name="Owner", role="owner")
            session.add(owner)
            await session.flush()
            self.client_id, self.owner_id = client.id, owner.id
            session.add(OwnerProfile(user_id=owner.id))
            await session.commit()
        self.bot = SimpleNamespace(send_message=AsyncMock())

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.path.unlink(missing_ok=True)

    async def test_lazy_identity_uses_database_id_and_honors_blocking(self):
        provider = OwnerGoogleTokens(self.sessions, settings=self.settings)
        with patch("app.services.booking_runtime.GoogleTokens") as tokens:
            tokens.return_value = AsyncMock(return_value="fake-token")
            self.assertEqual(await provider(force_refresh=True), "fake-token")
            self.assertEqual(tokens.call_args.kwargs["owner_user_id"], self.owner_id)
            self.assertNotEqual(self.owner_id, 777)
            tokens.return_value.assert_awaited_once_with(force_refresh=True)
            async with self.sessions() as session:
                owner = await session.get(User, self.owner_id)
                owner.status = "blocked"
                await session.commit()
            with self.assertRaisesRegex(ValueError, "GOOGLE_AUTH_REQUIRED"):
                await provider()
            self.assertEqual(tokens.call_count, 1)

    async def test_missing_owner_does_not_request_token_and_can_register_later(self):
        settings = self.settings.model_copy(update={"owner_telegram_id": 888})
        provider = OwnerGoogleTokens(self.sessions, settings=settings)
        with patch("app.services.booking_runtime.GoogleTokens") as tokens:
            tokens.return_value = AsyncMock(return_value="fake-token")
            with self.assertRaisesRegex(ValueError, "GOOGLE_AUTH_REQUIRED"):
                await provider()
            tokens.assert_not_called()
            async with self.sessions() as session:
                session.add(User(telegram_id=888, full_name="Later", role="owner"))
                await session.commit()
            self.assertEqual(await provider(), "fake-token")

    async def test_command_to_durable_notification_uses_composed_runtime(self):
        gateway = SimpleNamespace(busy=AsyncMock())
        runtime = build_booking_runtime(self.sessions, settings=self.settings, bot=self.bot,
            environment="test", clock=lambda: self.now, gateway=gateway)
        self.assertFalse(runtime.running)
        self.assertEqual(len(runtime.worker.handlers), 21)
        self.assertIs(runtime.commands.availability.gateway, gateway)
        key = uuid.uuid4().hex
        payload = dict(actor_id=self.client_id, command_id=key,
            start_at=datetime(2026, 10, 26, 7, tzinfo=timezone.utc),
            title="Consultation", settings_version=1)
        result = await runtime.commands.create_request(**payload)
        self.assertEqual(await runtime.commands.create_request(**payload), result)
        # Construction/commands neither start delivery nor call disconnected Google.
        self.bot.send_message.assert_not_awaited()
        gateway.busy.assert_not_awaited()
        while await runtime.worker.run_once():
            pass
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.call_args.args[0], 777)
        async with self.sessions() as session:
            events = (await session.scalars(select(OutboxEvent).order_by(OutboxEvent.id))).all()
            self.assertEqual(sorted(e.state for e in events), ["sent", "skipped"])
        await runtime.close()

    async def test_explicit_environment_required_before_start(self):
        for environment in (None, 123, "", "invalid env"):
            with self.assertRaises(ValueError):
                build_booking_runtime(self.sessions, settings=self.settings,
                    bot=self.bot, environment=environment)


if __name__ == "__main__":
    unittest.main()
