"""Telegram transport selection and authenticated, awaited webhook dispatch."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import json
import secrets

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod
from aiogram.types import Update
from pydantic import ValidationError

from app.config import Settings

WEBHOOK_PATH = "/telegram/webhook"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


@dataclass
class TelegramState:
    ready: bool = False


TELEGRAM_STATE = web.AppKey("telegram_state", TelegramState)


def register_webhook(app: web.Application, dispatcher: Dispatcher, bot: Bot, settings: Settings) -> None:
    # One connection and a process-local lock keep FSM steps in order. The small cache
    # absorbs retries while this process is alive; it does not promise exactly-once delivery.
    lock = asyncio.Lock()
    completed: OrderedDict[int, None] = OrderedDict()

    async def handle(request: web.Request) -> web.Response:
        supplied = request.headers.get(SECRET_HEADER, "").encode("utf-8", errors="replace")
        if not secrets.compare_digest(supplied, settings.telegram_webhook_secret.encode("ascii")):
            return web.Response(status=401, text="Unauthorized")
        if not app[TELEGRAM_STATE].ready:
            return web.Response(status=503, text="Starting")
        try:
            payload = await request.json()
            update = Update.model_validate(payload, context={"bot": bot})
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
            return web.Response(status=400, text="Invalid update")
        async with lock:
            if update.update_id not in completed:
                # Acknowledge only after dispatch. Exceptions return 500 so Telegram can retry.
                result = await dispatcher.feed_update(bot, update)
                if isinstance(result, TelegramMethod):
                    await bot(result)
                completed[update.update_id] = None
                if len(completed) > 1024:
                    completed.popitem(last=False)
        return web.json_response({})

    app.router.add_post(WEBHOOK_PATH, handle)


async def configure_transport(bot: Bot, dispatcher: Dispatcher, settings: Settings) -> None:
    for attempt in range(3):
        try:
            if settings.bot_mode == "webhook":
                await bot.set_webhook(
                    url=settings.telegram_webhook_url,
                    secret_token=settings.telegram_webhook_secret,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                    max_connections=1,
                    drop_pending_updates=False,
                    request_timeout=15,
                )
                info = await bot.get_webhook_info(request_timeout=15)
                if info.url != settings.telegram_webhook_url:
                    raise RuntimeError("Telegram webhook registration could not be verified")
            else:
                info = await bot.get_webhook_info(request_timeout=15)
                if info.url:
                    raise RuntimeError(
                        "Telegram webhook is active; polling was not started. "
                        "For an intentional rollback, deleteWebhook with drop_pending_updates=False first."
                    )
                await bot.me()  # Verify the token and warm the Dispatcher cache before readiness.
            return
        except TelegramNetworkError:
            if attempt == 2:
                raise
            await asyncio.sleep(attempt + 1)
