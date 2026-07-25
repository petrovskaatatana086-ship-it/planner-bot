import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.domain.email import is_valid_email
from app.services.email import (
    email_notifications_enabled,
    send_booking_approved_email,
    send_meeting_cancelled_email,
    send_meeting_reschedule_rejected_email,
)


class EmailValidationTest(unittest.TestCase):
    def test_valid_email(self) -> None:
        for email in [
            "client@example.com",
            "name.surname+tag@example.co",
            "a@domain.ru",
        ]:
            with self.subTest(email=email):
                self.assertTrue(is_valid_email(email))

    def test_invalid_email(self) -> None:
        for email in [
            "",
            "client",
            "client@",
            "@example.com",
            "client example@example.com",
            "client@@example.com",
            "client@example",
            "client@example..com",
        ]:
            with self.subTest(email=email):
                self.assertFalse(is_valid_email(email))


class EmailNotificationServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="bot@example.com",
            smtp_password="secret",
            smtp_from_email="bot@example.com",
            smtp_from_name="Planner",
            smtp_use_tls=True,
            smtp_use_ssl=False,
        )
        self.client = SimpleNamespace(
            email="client@example.com",
            full_name="Иван Клиент",
        )
        self.request = SimpleNamespace(
            id=12,
            start_at=datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc),
        )
        self.meeting = SimpleNamespace(
            id=7,
            title="Созвон по проекту",
            start_at=datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc),
            google_meet_url="https://meet.google.com/test-room",
        )

    def test_email_notifications_enabled_requires_full_smtp_config(self) -> None:
        self.assertTrue(email_notifications_enabled(self.settings))

        settings_without_password = SimpleNamespace(**self.settings.__dict__)
        settings_without_password.smtp_password = ""
        self.assertFalse(email_notifications_enabled(settings_without_password))

    async def test_approved_email_contains_time_and_meet_link(self) -> None:
        with patch("app.services.email._send_email", new_callable=AsyncMock) as send_mock:
            send_mock.return_value = True

            await send_booking_approved_email(
                self.settings,
                client=self.client,
                request=self.request,
                meeting=self.meeting,
            )

        self.assertEqual(send_mock.await_count, 1)
        _, kwargs = send_mock.await_args
        self.assertEqual(kwargs["to_email"], "client@example.com")
        self.assertEqual(kwargs["subject"], "Заявка #12 подтверждена")
        self.assertIn("28.07.2026 12:00 МСК", kwargs["body"])
        self.assertIn("https://meet.google.com/test-room", kwargs["body"])

    async def test_cancellation_email_contains_reason(self) -> None:
        with patch("app.services.email._send_email", new_callable=AsyncMock) as send_mock:
            send_mock.return_value = True

            await send_meeting_cancelled_email(
                self.settings,
                client=self.client,
                meeting=self.meeting,
                reason="Специалист заболел",
            )

        _, kwargs = send_mock.await_args
        self.assertEqual(kwargs["subject"], "Встреча #7 отменена")
        self.assertIn("Специалист заболел", kwargs["body"])

    async def test_reschedule_rejection_email_contains_current_time(self) -> None:
        with patch("app.services.email._send_email", new_callable=AsyncMock) as send_mock:
            send_mock.return_value = True

            await send_meeting_reschedule_rejected_email(
                self.settings,
                client=self.client,
                meeting=self.meeting,
                reason="На это время уже есть другая встреча",
            )

        _, kwargs = send_mock.await_args
        self.assertEqual(kwargs["subject"], "Запрос на перенос встречи #7 отклонен")
        self.assertIn("28.07.2026 12:00 МСК", kwargs["body"])
        self.assertIn("другая встреча", kwargs["body"])


if __name__ == "__main__":
    unittest.main()
