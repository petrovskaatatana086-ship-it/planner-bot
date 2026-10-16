import asyncio
import unittest
from pathlib import Path

from app.config import Settings
from app.db.repositories import (
    ensure_user_stub,
    list_clients,
    set_client_status,
)
from app.db.session import create_schema, make_sessionmaker


class ClientManagementRepositoryTest(unittest.TestCase):
    def test_list_clients_excludes_owner_and_supports_blocking(self) -> None:
        async def run() -> None:
            db_path = Path(".test_client_management.db")
            if db_path.exists():
                db_path.unlink()
            try:
                settings = Settings(
                    BOT_TOKEN="123:abc",
                    OWNER_TELEGRAM_ID=777,
                    DATABASE_URL="sqlite+aiosqlite:///./.test_client_management.db",
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
                    client = await ensure_user_stub(
                        session,
                        telegram_id=100,
                        owner_telegram_id=777,
                        full_name="Client One",
                        username="client1",
                    )
                    second_client = await ensure_user_stub(
                        session,
                        telegram_id=101,
                        owner_telegram_id=777,
                        full_name="Client Two",
                        username="client2",
                    )
                    await set_client_status(
                        session,
                        client=second_client,
                        status="blocked",
                        actor_user_id=owner.id,
                    )
                    await session.commit()

                async with session_factory() as session:
                    clients = await list_clients(session)

                self.assertEqual(len(clients), 2)
                self.assertEqual(clients[0].id, client.id)
                self.assertEqual(clients[0].status, "active")
                self.assertEqual(clients[1].id, second_client.id)
                self.assertEqual(clients[1].status, "blocked")
                self.assertNotIn(owner.id, {item.id for item in clients})
            finally:
                if db_path.exists():
                    db_path.unlink()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
