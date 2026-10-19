"""Run a separate development bot using .env.local only."""

import asyncio
import os
from pathlib import Path

from dotenv import dotenv_values

from app.config import Settings, get_settings


def local_settings() -> Settings:
    root = Path(__file__).resolve().parent
    os.chdir(root)
    values = dotenv_values(root / ".env.local", interpolate=False)
    token = values.get("BOT_TOKEN", "")
    if not token or token == "PASTE_TEST_BOT_TOKEN_HERE":
        raise SystemExit("Add a separate test bot token to .env.local (BOT_TOKEN).")
    production_token = dotenv_values(root / ".env", interpolate=False).get("BOT_TOKEN")
    if token == production_token:
        raise SystemExit("Use a separate test bot: this token matches the main .env.")
    # Explicit values for every field prevent inheritance of production environment.
    defaults = {
        field.alias: field.default
        for field in Settings.model_fields.values()
        if not field.is_required()
    }
    defaults.update({key: value for key, value in values.items() if value is not None})
    defaults.update(
        BOT_MODE="polling",
        WEB_HOST="127.0.0.1",
        WEB_PORT=8001,
        DATABASE_URL="sqlite+aiosqlite:///./planner.local.db",
        TELEGRAM_WEBHOOK_URL="",
        TELEGRAM_WEBHOOK_SECRET="",
    )
    return Settings(_env_file=None, **defaults)


if __name__ == "__main__":
    settings = local_settings()
    get_settings.cache_clear()
    # app.main imports this callable when loaded below.
    import app.config

    app.config.get_settings = lambda: settings
    from app.main import main

    asyncio.run(main())
