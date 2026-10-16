import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import make_mocked_request
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetWebhookInfo
from pydantic import ValidationError

from app.config import Settings
from app.telegram import TELEGRAM_STATE, SECRET_HEADER, WEBHOOK_PATH, configure_transport
from app.web import create_web_app, health

URL = 'https://calendar.example.com/telegram/webhook'
SECRET = 'test_webhook_secret_' + 'x' * 32


def settings(**kwargs):
    return Settings(_env_file=None, BOT_TOKEN='123:abc', OWNER_TELEGRAM_ID=1, **kwargs)


class WebhookSettingsTest(unittest.TestCase):
    def test_polling_remains_the_default(self):
        self.assertEqual(settings().bot_mode, 'polling')

    def test_webhook_rejects_insecure_or_incomplete_configuration(self):
        for url, secret in [
            ('http://calendar.example.com/telegram/webhook', SECRET),
            (URL, ''), (URL, 'too-short'), (URL, 'bad+' * 20),
            (URL + '?token=123:abc', SECRET),
            ('https://user:password@calendar.example.com/telegram/webhook', SECRET),
        ]:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                settings(BOT_MODE='webhook', TELEGRAM_WEBHOOK_URL=url, TELEGRAM_WEBHOOK_SECRET=secret)


class TelegramTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_registers_webhook_without_dropping_pending_updates(self):
        bot = AsyncMock()
        bot.get_webhook_info.return_value = SimpleNamespace(url=URL)
        dispatcher = MagicMock()
        dispatcher.resolve_used_update_types.return_value = ['message', 'callback_query']
        config = settings(BOT_MODE='webhook', TELEGRAM_WEBHOOK_URL=URL, TELEGRAM_WEBHOOK_SECRET=SECRET)
        await configure_transport(bot, dispatcher, config)
        bot.set_webhook.assert_awaited_once_with(
            url=URL, secret_token=SECRET, allowed_updates=['message', 'callback_query'],
            max_connections=1, drop_pending_updates=False, request_timeout=15,
        )
        bot.delete_webhook.assert_not_called()

    async def test_accidental_polling_cannot_delete_an_active_webhook(self):
        bot = AsyncMock()
        bot.get_webhook_info.return_value = SimpleNamespace(url=URL)
        with self.assertRaisesRegex(RuntimeError, 'webhook is active'):
            await configure_transport(bot, MagicMock(), settings())
        bot.delete_webhook.assert_not_called()
        bot.set_webhook.assert_not_called()
        bot.me.assert_not_called()

    async def test_polling_checks_telegram_before_readiness(self):
        bot = AsyncMock()
        bot.get_webhook_info.return_value = SimpleNamespace(url='')
        await configure_transport(bot, MagicMock(), settings())
        bot.me.assert_awaited_once()
        bot.set_webhook.assert_not_called()

    async def test_transient_network_error_is_retried(self):
        bot = AsyncMock()
        bot.get_webhook_info.side_effect = [
            TelegramNetworkError(method=GetWebhookInfo(), message='Temporary timeout'),
            SimpleNamespace(url=''),
        ]
        with patch('app.telegram.asyncio.sleep', new_callable=AsyncMock):
            await configure_transport(bot, MagicMock(), settings())
        self.assertEqual(bot.get_webhook_info.await_count, 2)


class WebhookHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = settings(BOT_MODE='webhook', TELEGRAM_WEBHOOK_URL=URL, TELEGRAM_WEBHOOK_SECRET=SECRET)
        self.bot = MagicMock()
        self.dispatcher = MagicMock()
        self.dispatcher.feed_update = AsyncMock(return_value=None)
        self.app = create_web_app(self.config, MagicMock(), self.bot, self.dispatcher)
        self.app[TELEGRAM_STATE].ready = True
        self.handle = next(r.handler for r in self.app.router.routes() if r.method == 'POST')

    def request(self, payload=None, secret=SECRET):
        request = make_mocked_request('POST', WEBHOOK_PATH, headers={SECRET_HEADER: secret}, app=self.app)
        request.json = AsyncMock(return_value=payload if payload is not None else {'update_id': 12345})
        return request

    async def test_missing_or_wrong_secret_is_rejected_before_reading_body(self):
        for secret in ['', 'wrong', 'неверный']:
            request = self.request(secret=secret)
            response = await self.handle(request)
            self.assertEqual(response.status, 401)
            request.json.assert_not_called()
        self.dispatcher.feed_update.assert_not_called()

    async def test_startup_does_not_acknowledge_updates(self):
        self.app[TELEGRAM_STATE].ready = False
        self.assertEqual((await self.handle(self.request())).status, 503)
        self.dispatcher.feed_update.assert_not_called()
        response = await health(make_mocked_request('GET', '/health', app=self.app))
        self.assertEqual(response.status, 503)

    async def test_valid_update_is_dispatched_and_acknowledged(self):
        response = await self.handle(self.request())
        self.assertEqual(response.status, 200)
        update = self.dispatcher.feed_update.call_args.args[1]
        self.assertEqual(update.update_id, 12345)
        response = await health(make_mocked_request('GET', '/health', app=self.app))
        self.assertEqual(json.loads(response.text), {'status': 'ok', 'transport': 'webhook'})

    async def test_invalid_json_and_update_are_rejected(self):
        request = self.request()
        request.json.side_effect = json.JSONDecodeError('invalid', '', 0)
        self.assertEqual((await self.handle(request)).status, 400)
        self.assertEqual((await self.handle(self.request({'invalid': True}))).status, 400)
        self.dispatcher.feed_update.assert_not_called()

    async def test_inflight_retry_is_not_dispatched_twice_or_acked_early(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def dispatch(*_):
            started.set()
            await finish.wait()
        self.dispatcher.feed_update.side_effect = dispatch
        first = asyncio.create_task(self.handle(self.request()))
        await started.wait()
        duplicate = asyncio.create_task(self.handle(self.request()))
        self.assertFalse(first.done())
        finish.set()
        responses = await asyncio.gather(first, duplicate)
        self.assertTrue(all(r.status == 200 for r in responses))
        self.dispatcher.feed_update.assert_awaited_once()

    async def test_failed_dispatch_can_be_retried(self):
        self.dispatcher.feed_update.side_effect = [RuntimeError('temporary failure'), None]
        with self.assertRaises(RuntimeError):
            await self.handle(self.request())
        self.assertEqual((await self.handle(self.request())).status, 200)
        self.assertEqual(self.dispatcher.feed_update.await_count, 2)

    async def test_polling_app_has_no_webhook_route(self):
        app = create_web_app(settings(), MagicMock(), self.bot)
        self.assertFalse(any(r.resource.canonical == WEBHOOK_PATH for r in app.router.routes()))


class MainTransportTest(unittest.IsolatedAsyncioTestCase):
    async def check_mode(self, mode):
        from app.main import main
        config = settings(BOT_MODE=mode, TELEGRAM_WEBHOOK_URL=URL, TELEGRAM_WEBHOOK_SECRET=SECRET)
        bot = MagicMock()
        bot.session.close = AsyncMock()
        dispatcher = MagicMock()
        dispatcher.workflow_data = {}
        dispatcher.start_polling = AsyncMock()
        dispatcher.emit_shutdown = AsyncMock()
        runner = MagicMock()
        runner.setup = AsyncMock()
        runner.cleanup = AsyncMock()
        site = MagicMock()
        site.start = AsyncMock()
        with patch('app.main.get_settings', return_value=config), \
                patch('app.main.configure_logging'), \
                patch('app.main.create_schema', new_callable=AsyncMock), \
                patch('app.main.make_sessionmaker'), \
                patch('app.main.Bot', return_value=bot), \
                patch('app.main.Dispatcher', return_value=dispatcher), \
                patch('app.main.web.AppRunner', return_value=runner), \
                patch('app.main.web.TCPSite', return_value=site), \
                patch('app.main.configure_transport', new_callable=AsyncMock), \
                patch('app.main.serve_webhook', new_callable=AsyncMock) as serve:
            await main()
        if mode == 'webhook':
            dispatcher.start_polling.assert_not_called()
            serve.assert_awaited_once()
            dispatcher.emit_shutdown.assert_awaited_once()
        else:
            dispatcher.start_polling.assert_awaited_once_with(bot, close_bot_session=False)
            serve.assert_not_called()
        runner.cleanup.assert_awaited_once()
        bot.session.close.assert_awaited_once()

    async def test_webhook_does_not_start_polling(self):
        await self.check_mode('webhook')

    async def test_polling_mode_remains_available(self):
        await self.check_mode('polling')
