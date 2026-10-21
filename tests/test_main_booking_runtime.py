import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

from app.config import Settings
from app.main import main, supervise_transport
from app.services.booking_runtime import BookingRuntime


class MainBookingRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def config(self, **overrides):
        return Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
            BOOKING_RUNTIME_ENABLED=True, CALENDAR_ENVIRONMENT="test", **overrides)

    async def check_main_mode(self, mode):
        events = []
        runtime = SimpleNamespace()
        async def start():
            events.append("start")
        async def close():
            events.append("runtime-close")
        runtime.start = AsyncMock(side_effect=start)
        runtime.close = AsyncMock(side_effect=close)
        runtime.wait = AsyncMock(side_effect=asyncio.Event().wait)
        bot = MagicMock()
        async def bot_close():
            events.append("bot-close")
        bot.session.close = AsyncMock(side_effect=bot_close)
        dispatcher = MagicMock()
        dispatcher.workflow_data = {}
        async def poll(*args, **kwargs):
            events.append("poll")
        dispatcher.start_polling = AsyncMock(side_effect=poll)
        dispatcher.emit_shutdown = AsyncMock()
        runner = MagicMock(setup=AsyncMock())
        async def cleanup():
            events.append("http-cleanup")
        runner.cleanup = AsyncMock(side_effect=cleanup)
        site = MagicMock(start=AsyncMock())
        with patch("app.main.get_settings", return_value=self.config(BOT_MODE=mode,
                TELEGRAM_WEBHOOK_URL="https://calendar.example.com/telegram/webhook",
                TELEGRAM_WEBHOOK_SECRET="x"*40)), \
                patch("app.main.configure_logging"), \
                patch("app.main.create_schema", new_callable=AsyncMock), \
                patch("app.main.verify_booking_schema", new_callable=AsyncMock) as verify, \
                patch("app.main.make_sessionmaker") as sessions, \
                patch("app.main.Bot", return_value=bot), \
                patch("app.main.Dispatcher", return_value=dispatcher), \
                patch("app.main.web.AppRunner", return_value=runner), \
                patch("app.main.web.TCPSite", return_value=site), \
                patch("app.main.configure_transport", new_callable=AsyncMock), \
                patch("app.main.serve_webhook", new_callable=AsyncMock, side_effect=poll) as serve, \
                patch("app.main.build_booking_runtime", return_value=runtime) as build:
            await main()
        verify.assert_awaited_once_with(sessions.return_value)
        self.assertEqual(build.call_args.kwargs["environment"], "test")
        self.assertIn(unittest.mock.call("booking_runtime", runtime), dispatcher.__setitem__.call_args_list)
        self.assertEqual(events, ["start", "poll", "http-cleanup", "runtime-close", "bot-close"])
        if mode == "webhook":
            serve.assert_awaited_once()
            dispatcher.start_polling.assert_not_called()
            dispatcher.emit_shutdown.assert_awaited_once()
        else:
            dispatcher.start_polling.assert_awaited_once_with(bot, close_bot_session=False, handle_as_tasks=False)

    async def test_main_polling_runtime_order(self):
        await self.check_main_mode("polling")

    async def test_main_webhook_runtime_order(self):
        await self.check_main_mode("webhook")

    async def test_schema_failure_prevents_bot_and_runtime_startup(self):
        with patch("app.main.get_settings", return_value=self.config()), \
                patch("app.main.configure_logging"), \
                patch("app.main.create_schema", new_callable=AsyncMock), \
                patch("app.main.make_sessionmaker"), \
                patch("app.main.verify_booking_schema", new_callable=AsyncMock,
                      side_effect=RuntimeError("outdated schema")), \
                patch("app.main.Bot") as bot, \
                patch("app.main.build_booking_runtime") as build:
            with self.assertRaisesRegex(RuntimeError, "outdated schema"):
                await main()
        bot.assert_not_called()
        build.assert_not_called()

    async def test_worker_failure_cancels_transport(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def worker(stop):
            await entered.wait()
            raise RuntimeError("worker failed")
        async def transport():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        runtime = BookingRuntime(commands=None, worker=SimpleNamespace(run=worker))
        await runtime.start()
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            await supervise_transport(runtime, transport())
        self.assertTrue(cancelled.is_set())
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            await runtime.close()

    async def test_normal_transport_exit_does_not_cancel_delivery(self):
        entered = asyncio.Event()
        async def worker(stop):
            entered.set()
            await stop.wait()
        async def transport():
            await entered.wait()
        runtime = BookingRuntime(commands=None, worker=SimpleNamespace(run=worker))
        await runtime.start()
        await supervise_transport(runtime, transport())
        self.assertTrue(runtime.running)
        await runtime.close()

    async def test_enabled_runtime_requires_explicit_environment(self):
        for environment in ("", "bad env"):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
                    BOOKING_RUNTIME_ENABLED=True, CALENDAR_ENVIRONMENT=environment)
        self.assertFalse(Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777).booking_runtime_enabled)


if __name__ == "__main__":
    unittest.main()
