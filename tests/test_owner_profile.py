import asyncio
import unittest
from pathlib import Path

from app.config import Settings
from app.db.repositories import (
    ensure_user_stub,
    get_owner_profile,
    update_owner_profile_field,
)
from app.db.session import create_schema, make_sessionmaker


class OwnerProfileRepositoryTest(unittest.TestCase):
    def test_owner_profile_is_created_and_updated(self) -> None:
        async def run() -> None:
            db_path = Path(".test_owner_profile.db")
            if db_path.exists():
                db_path.unlink()
            try:
                settings = Settings(
                    BOT_TOKEN="123:abc",
                    OWNER_TELEGRAM_ID=777,
                    DATABASE_URL="sqlite+aiosqlite:///./.test_owner_profile.db",
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
                    profile = await get_owner_profile(session, owner.id)

                    self.assertIsNotNone(profile)
                    self.assertEqual(profile.default_duration, 60)

                    updated = await update_owner_profile_field(
                        session,
                        owner=owner,
                        field="default_duration",
                        value=90,
                    )

                    self.assertEqual(updated.default_duration, 90)
            finally:
                if db_path.exists():
                    db_path.unlink()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
