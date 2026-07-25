import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from app.bot.handlers import router
from app.config import get_settings
from app.db.session import create_schema, make_sessionmaker
from app.logging_config import configure_logging
from app.web import create_web_app


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

    web_app = create_web_app(settings, session_factory, bot)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, settings.web_host, settings.web_port)
    await site.start()
    logger.info("Web callback server started on %s:%s", settings.web_host, settings.web_port)

    logger.info("Bot polling started")
    try:
        await dispatcher.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
