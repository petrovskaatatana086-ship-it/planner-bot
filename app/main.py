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
from app.services.booking_runtime import build_booking_runtime, verify_booking_schema


async def supervise_transport(runtime, transport):
    """Stop accepting updates if the durable delivery worker fails."""
    transport_task = asyncio.create_task(transport)
    monitor = asyncio.create_task(runtime.wait())
    try:
        done, _ = await asyncio.wait({transport_task, monitor}, return_when=asyncio.FIRST_COMPLETED)
        if monitor in done:
            await monitor
            raise RuntimeError("Booking worker stopped unexpectedly")
        return await transport_task
    finally:
        transport_task.cancel()
        monitor.cancel()
        await asyncio.gather(transport_task, monitor, return_exceptions=True)


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
    if settings.booking_runtime_enabled:
        await verify_booking_schema(session_factory)

    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["settings"] = settings
    dispatcher["session_factory"] = session_factory
    dispatcher.include_router(router)
    runtime = None

    if settings.booking_runtime_enabled:
        runtime = build_booking_runtime(session_factory, settings=settings, bot=bot,
            environment=settings.calendar_environment)
        dispatcher["booking_runtime"] = runtime
    web_app = create_web_app(settings, session_factory, bot, dispatcher, booking_runtime=runtime)
    # OAuth callback URLs contain one-time codes and state; keep them out of logs.
    runner = web.AppRunner(web_app, access_log=None, max_field_size=20000)
    webhook_started = False
    try:
        await runner.setup()
        site = web.TCPSite(runner, settings.web_host, settings.web_port)
        await site.start()
        logger.info("Web callback server started on %s:%s", settings.web_host, settings.web_port)
        await configure_transport(bot, dispatcher, settings)
        if settings.booking_runtime_enabled:
            await runtime.start()
        web_app[TELEGRAM_STATE].ready = True
        if settings.bot_mode == "webhook":
            logger.info("Bot webhook started")
            webhook_started = True
            transport = serve_webhook(dispatcher, bot)
        else:
            logger.info("Bot polling started")
            # Process sequentially so shutdown cannot leave detached update
            # handlers writing commands after the worker/Bot have been closed.
            transport = dispatcher.start_polling(bot, close_bot_session=False, handle_as_tasks=False)
        if runtime is not None:
            await supervise_transport(runtime, transport)
        else:
            await transport
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
                try:
                    if runtime is not None:
                        await runtime.close()
                finally:
                    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
