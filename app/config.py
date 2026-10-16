from functools import lru_cache
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = Field(alias="BOT_TOKEN")
    owner_telegram_id: int = Field(alias="OWNER_TELEGRAM_ID")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./planner.db",
        alias="DATABASE_URL",
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = Field(default="", alias="GOOGLE_REDIRECT_URI")
    oauth_encryption_key: str = Field(default="", alias="OAUTH_ENCRYPTION_KEY")
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8000, alias="WEB_PORT")
    bot_mode: Literal["polling", "webhook"] = Field(default="polling", alias="BOT_MODE")
    telegram_webhook_url: str = Field(default="", alias="TELEGRAM_WEBHOOK_URL")
    telegram_webhook_secret: str = Field(default="", alias="TELEGRAM_WEBHOOK_SECRET", repr=False)
    smtp_host: str = Field(default="", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_username: str = Field(default="", alias="SMTP_USERNAME")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from_email: str = Field(default="", alias="SMTP_FROM_EMAIL")
    smtp_from_name: str = Field(default="Планировщик встреч", alias="SMTP_FROM_NAME")
    smtp_use_tls: bool = Field(default=True, alias="SMTP_USE_TLS")
    smtp_use_ssl: bool = Field(default=False, alias="SMTP_USE_SSL")

    @model_validator(mode="after")
    def validate_webhook_configuration(self):
        if self.bot_mode == "webhook":
            url = urlsplit(self.telegram_webhook_url)
            if (url.scheme != "https" or not url.hostname or url.username or url.password
                    or url.path != "/telegram/webhook" or url.query or url.fragment
                    or self.bot_token in self.telegram_webhook_url):
                raise ValueError("TELEGRAM_WEBHOOK_URL must be an HTTPS URL ending in /telegram/webhook")
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.telegram_webhook_secret):
                raise ValueError("TELEGRAM_WEBHOOK_SECRET must contain 32–256 URL-safe characters")
        return self

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", hide_input_in_errors=True,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
