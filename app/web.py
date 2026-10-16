import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.repositories import complete_google_oauth, fail_google_oauth
from app.integrations.google_oauth import (
    exchange_code_for_tokens,
    fetch_google_account_email,
    google_oauth_is_configured,
    parse_oauth_state,
)
from app.security import encrypt_secret
from app.telegram import TELEGRAM_STATE, TelegramState, register_webhook

logger = logging.getLogger(__name__)


def create_web_app(
    settings: Settings, session_factory: async_sessionmaker, bot: Bot,
    dispatcher: Dispatcher | None = None,
) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    app["session_factory"] = session_factory
    app["bot"] = bot
    app[TELEGRAM_STATE] = TelegramState()
    app.router.add_get("/google/oauth/callback", google_oauth_callback)
    app.router.add_get("/health", health)
    if settings.bot_mode == "webhook":
        if dispatcher is None:
            raise ValueError("Webhook mode requires a dispatcher")
        register_webhook(app, dispatcher, bot, settings)
    return app


async def health(request: web.Request) -> web.Response:
    ready = request.app[TELEGRAM_STATE].ready
    return web.json_response(
        {"status": "ok" if ready else "starting", "transport": request.app["settings"].bot_mode},
        status=200 if ready else 503,
    )


async def google_oauth_callback(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    session_factory: async_sessionmaker = request.app["session_factory"]
    bot: Bot = request.app["bot"]

    if request.query.get("error"):
        return web.Response(
            text=f"Google OAuth error: {request.query['error']}",
            status=400,
            content_type="text/plain",
        )

    code = request.query.get("code")
    state = request.query.get("state")
    if not code or not state:
        return web.Response(text="Missing code or state", status=400, content_type="text/plain")

    parsed_state = parse_oauth_state(state)
    if parsed_state is None:
        return web.Response(text="Invalid OAuth state", status=400, content_type="text/plain")
    owner_user_id, _nonce = parsed_state

    if not google_oauth_is_configured(settings):
        return web.Response(text="Google OAuth is not configured", status=500, content_type="text/plain")
    if not settings.oauth_encryption_key:
        return web.Response(text="OAUTH_ENCRYPTION_KEY is not configured", status=500, content_type="text/plain")

    try:
        token_response = await exchange_code_for_tokens(settings, code)
        google_email = await fetch_google_account_email(token_response.access_token)
        encrypted_access_token = encrypt_secret(
            token_response.access_token,
            settings.oauth_encryption_key,
        )
        encrypted_refresh_token = None
        if token_response.refresh_token:
            encrypted_refresh_token = encrypt_secret(
                token_response.refresh_token,
                settings.oauth_encryption_key,
            )

        async with session_factory() as session:
            await complete_google_oauth(
                session,
                owner_user_id=owner_user_id,
                oauth_state=state,
                encrypted_access_token=encrypted_access_token,
                encrypted_refresh_token=encrypted_refresh_token,
                expires_at=token_response.expires_at,
                google_email=google_email,
            )
            await session.commit()

        await _notify_owner(bot, settings, f"Google Calendar подключен: {google_email}")
        return web.Response(
            text="Google Calendar connected. You can return to Telegram.",
            content_type="text/plain",
        )
    except Exception as exc:
        logger.exception("Google OAuth callback failed")
        async with session_factory() as session:
            await fail_google_oauth(
                session,
                owner_user_id=owner_user_id,
                error=str(exc),
            )
            await session.commit()
        await _notify_owner(bot, settings, f"Ошибка подключения Google Calendar: {exc}")
        return web.Response(
            text="Google Calendar connection failed. Return to Telegram and try again.",
            status=400,
            content_type="text/plain",
        )


async def _notify_owner(bot: Bot, settings: Settings, text: str) -> None:
    try:
        await bot.send_message(settings.owner_telegram_id, text)
    except Exception:
        logger.exception("Failed to notify owner in Telegram")
