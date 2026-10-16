import asyncio
import logging
import signal
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from app.bot.handlers import router
from app.config import get_settings
from app.db.session import create_schema, make_sessionmaker
from app.logging_config import configure_logging
from app.web import create_web_app
from app.telegram import TELEGRAM_STATE, configure_transport


async def serve_webhook(dispatcher: Dispatcher, bot: Bot) -> None:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stopped.set)
            installed.append(signum)
    try:
        await dispatcher.emit_startup(bot=bot, dispatcher=dispatcher, **dispatcher.workflow_data)
        await stopped.wait()
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)

    await create_schema(settings)
    session_factory = make_sessionmaker(settings)

    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["settings"] = settings
    dispatcher["session_factory"] = session_factory
    dispatcher.include_router(router)

    web_app = create_web_app(settings, session_factory, bot, dispatcher)
    # OAuth callback URLs contain one-time codes and state; keep them out of logs.
    runner = web.AppRunner(web_app, access_log=None)
    webhook_started = False
    try:
        await runner.setup()
        site = web.TCPSite(runner, settings.web_host, settings.web_port)
        await site.start()
        logger.info("Web callback server started on %s:%s", settings.web_host, settings.web_port)
        await configure_transport(bot, dispatcher, settings)
        web_app[TELEGRAM_STATE].ready = True
        if settings.bot_mode == "webhook":
            logger.info("Bot webhook started")
            webhook_started = True
            await serve_webhook(dispatcher, bot)
        else:
            logger.info("Bot polling started")
            await dispatcher.start_polling(bot, close_bot_session=False)
    finally:
        web_app[TELEGRAM_STATE].ready = False
        # Drain HTTP handlers before closing the shared Bot client. Leave webhook
        # registered across restarts so Telegram retains updates during deployment.
        try:
            await runner.cleanup()
        finally:
            try:
                if webhook_started:
                    await dispatcher.emit_shutdown(bot=bot, dispatcher=dispatcher, **dispatcher.workflow_data)
            finally:
                await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
