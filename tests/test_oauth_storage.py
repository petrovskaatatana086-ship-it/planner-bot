import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.db.repositories import (
    complete_google_oauth,
    ensure_user_stub,
    get_google_oauth_credential,
    get_owner_profile,
    mark_google_oauth_pending,
    set_owner_google_calendar,
)
from app.db.session import create_schema, make_sessionmaker
from app.integrations.google_oauth import GoogleTokenResponse
from app.security import decrypt_secret, encrypt_secret, generate_encryption_key
from app.services.google import get_valid_google_access_token


class OAuthStorageTest(unittest.TestCase):
    def test_tokens_are_encrypted_and_oauth_becomes_active(self) -> None:
        async def run() -> None:
            db_path = Path(".test_oauth_storage.db")
            if db_path.exists():
                db_path.unlink()
            try:
                settings = Settings(
                    BOT_TOKEN="123:abc",
                    OWNER_TELEGRAM_ID=777,
                    DATABASE_URL="sqlite+aiosqlite:///./.test_oauth_storage.db",
                )
                await create_schema(settings)
                session_factory = make_sessionmaker(settings)
                key = generate_encryption_key()

                async with session_factory() as session:
                    owner = await ensure_user_stub(
                        session,
                        telegram_id=777,
                        owner_telegram_id=777,
                        full_name="Owner",
                        username=None,
                    )
                    await mark_google_oauth_pending(session, owner, oauth_state="1:test-state")
                    encrypted_access = encrypt_secret("access-token", key)
                    encrypted_refresh = encrypt_secret("refresh-token", key)

                    credential = await complete_google_oauth(
                        session,
                        owner_user_id=owner.id,
                        oauth_state="1:test-state",
                        encrypted_access_token=encrypted_access,
                        encrypted_refresh_token=encrypted_refresh,
                        expires_at=datetime.now(timezone.utc),
                        google_email="owner@gmail.com",
                    )
                    profile = await get_owner_profile(session, owner.id)
                    found = await get_google_oauth_credential(session, owner.id)

                    self.assertEqual(credential.status, "active")
                    self.assertEqual(found.status, "active")
                    self.assertEqual(profile.google_status, "active")
                    self.assertEqual(profile.google_email, "owner@gmail.com")
                    self.assertNotEqual(found.encrypted_access_token, "access-token")
                    self.assertEqual(decrypt_secret(found.encrypted_access_token, key), "access-token")
                    self.assertEqual(decrypt_secret(found.encrypted_refresh_token, key), "refresh-token")
                    self.assertIsNone(found.oauth_state)
            finally:
                if db_path.exists():
                    db_path.unlink()

        asyncio.run(run())

    def test_expired_access_token_is_refreshed(self) -> None:
        async def run() -> None:
            db_path = Path(".test_oauth_refresh.db")
            if db_path.exists():
                db_path.unlink()
            try:
                settings = Settings(
                    BOT_TOKEN="123:abc",
                    OWNER_TELEGRAM_ID=777,
                    DATABASE_URL="sqlite+aiosqlite:///./.test_oauth_refresh.db",
                    GOOGLE_CLIENT_ID="client",
                    GOOGLE_CLIENT_SECRET="secret",
                    GOOGLE_REDIRECT_URI="https://example.com/google/oauth/callback",
                    OAUTH_ENCRYPTION_KEY=generate_encryption_key(),
                )
                await create_schema(settings)
                session_factory = make_sessionmaker(settings)

                async with session_factory() as session:
                    owner = await ensure_user_stub(
                        session,
                        telegram_id=777,
                        owner_telegram_id=777,
                        full_name="Owner",
                        username=None,
                    )
                    await mark_google_oauth_pending(session, owner, oauth_state="1:test-state")
                    await complete_google_oauth(
                        session,
                        owner_user_id=owner.id,
                        oauth_state="1:test-state",
                        encrypted_access_token=encrypt_secret(
                            "old-access-token",
                            settings.oauth_encryption_key,
                        ),
                        encrypted_refresh_token=encrypt_secret(
                            "refresh-token",
                            settings.oauth_encryption_key,
                        ),
                        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                        google_email="owner@gmail.com",
                    )

                    async def fake_refresh(_settings, refresh_token):
                        self.assertEqual(refresh_token, "refresh-token")
                        return GoogleTokenResponse(
                            access_token="new-access-token",
                            refresh_token=None,
                            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                            scope="",
                            token_type="Bearer",
                            id_token=None,
                        )

                    with patch("app.services.google.refresh_access_token", fake_refresh):
                        token = await get_valid_google_access_token(
                            session,
                            settings=settings,
                            owner_user_id=owner.id,
                        )

                    found = await get_google_oauth_credential(session, owner.id)
                    self.assertEqual(token, "new-access-token")
                    self.assertEqual(
                        decrypt_secret(found.encrypted_access_token, settings.oauth_encryption_key),
                        "new-access-token",
                    )

            finally:
                if db_path.exists():
                    db_path.unlink()

        asyncio.run(run())

    def test_owner_calendar_can_be_saved(self) -> None:
        async def run() -> None:
            db_path = Path(".test_calendar_selection.db")
            if db_path.exists():
                db_path.unlink()
            try:
                settings = Settings(
                    BOT_TOKEN="123:abc",
                    OWNER_TELEGRAM_ID=777,
                    DATABASE_URL="sqlite+aiosqlite:///./.test_calendar_selection.db",
                )
                await create_schema(settings)
                session_factory = make_sessionmaker(settings)

                async with session_factory() as session:
                    owner = await ensure_user_stub(
                        session,
                        telegram_id=777,
                        owner_telegram_id=777,
                        full_name="Owner",
                        username=None,
                    )
                    profile = await set_owner_google_calendar(
                        session,
                        owner_user_id=owner.id,
                        calendar_id="primary",
                    )

                    self.assertEqual(profile.calendar_id, "primary")
            finally:
                if db_path.exists():
                    db_path.unlink()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
