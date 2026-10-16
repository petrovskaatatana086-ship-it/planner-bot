import asyncio
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from app.config import Settings
from app.db.repositories import ensure_user_stub, get_google_oauth_credential, mark_google_oauth_pending
from app.db.session import create_schema, make_sessionmaker
from app.integrations.google_oauth import (
    build_google_oauth_link,
    fetch_google_account_email,
    google_oauth_is_configured,
    parse_oauth_state,
)


class GoogleOAuthTest(unittest.TestCase):
    def test_google_oauth_link_contains_required_params(self) -> None:
        settings = Settings(
            BOT_TOKEN="123:abc",
            OWNER_TELEGRAM_ID=1,
            GOOGLE_CLIENT_ID="client-id",
            GOOGLE_CLIENT_SECRET="secret",
            GOOGLE_REDIRECT_URI="https://example.com/google/oauth/callback",
        )

        self.assertTrue(google_oauth_is_configured(settings))
        link = build_google_oauth_link(settings, owner_user_id=10)
        parsed = urlparse(link.url)
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.netloc, "accounts.google.com")
        self.assertEqual(params["client_id"], ["client-id"])
        self.assertEqual(params["redirect_uri"], ["https://example.com/google/oauth/callback"])
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["access_type"], ["offline"])
        self.assertTrue(params["state"][0].startswith("10:"))
        self.assertIn("https://www.googleapis.com/auth/calendar.events", params["scope"][0])

    def test_oauth_state_is_parsed(self) -> None:
        self.assertEqual(parse_oauth_state("10:abc"), (10, "abc"))
        self.assertIsNone(parse_oauth_state("broken"))
        self.assertIsNone(parse_oauth_state("abc:nonce"))

    def test_google_oauth_pending_record_is_created(self) -> None:
        async def run() -> None:
            db_path = Path(".test_google_oauth.db")
            if db_path.exists():
                db_path.unlink()
            try:
                settings = Settings(
                    BOT_TOKEN="123:abc",
                    OWNER_TELEGRAM_ID=777,
                    DATABASE_URL="sqlite+aiosqlite:///./.test_google_oauth.db",
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
                    credential = await mark_google_oauth_pending(session, owner)
                    found = await get_google_oauth_credential(session, owner.id)

                    self.assertEqual(credential.status, "pending")
                    self.assertIsNotNone(found)
                    self.assertEqual(found.provider, "google")
            finally:
                if db_path.exists():
                    db_path.unlink()

        asyncio.run(run())

    def test_google_account_email_is_loaded_from_userinfo(self) -> None:
        class FakeResponse:
            status = 200

            async def json(self, content_type=None):
                return {"email": "owner@gmail.com"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

        class FakeSession:
            def get(self, url, headers=None, timeout=None):
                self.url = url
                self.headers = headers
                return FakeResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

        async def run() -> None:
            with patch("app.integrations.google_oauth.ClientSession", return_value=FakeSession()):
                email = await fetch_google_account_email("access-token")
            self.assertEqual(email, "owner@gmail.com")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
