import unittest

from app.domain.roles import CLIENT_ROLE, OWNER_ROLE, resolve_role


class RoleResolutionTest(unittest.TestCase):
    def test_owner_role_is_resolved_by_telegram_id(self) -> None:
        self.assertEqual(resolve_role(telegram_id=123, owner_telegram_id=123), OWNER_ROLE)

    def test_client_role_is_default(self) -> None:
        self.assertEqual(resolve_role(telegram_id=456, owner_telegram_id=123), CLIENT_ROLE)


if __name__ == "__main__":
    unittest.main()
