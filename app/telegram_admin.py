"""Read webhook status or intentionally remove it without dropping pending updates."""

import argparse
import asyncio
import json

from aiogram import Bot

from app.config import get_settings


async def run(command: str) -> None:
    settings = get_settings()
    bot = Bot(settings.bot_token)
    try:
        if command == "delete-webhook":
            await bot.delete_webhook(drop_pending_updates=False, request_timeout=15)
            print("Webhook removed; pending updates preserved")
        else:
            info = await bot.get_webhook_info(request_timeout=15)
            print(json.dumps({
                "configured_mode": settings.bot_mode,
                "url": info.url,
                "pending_update_count": info.pending_update_count,
                "max_connections": info.max_connections,
                "last_error_message": info.last_error_message,
                "last_error_date": info.last_error_date.isoformat() if info.last_error_date else None,
                "allowed_updates": info.allowed_updates,
            }))
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "delete-webhook"])
    args = parser.parse_args()
    asyncio.run(run(args.command))


if __name__ == "__main__":
    main()
