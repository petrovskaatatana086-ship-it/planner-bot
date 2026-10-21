import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.bot.keyboards import (
    booking_comment_keyboard,
    booking_confirmation_keyboard,
    booking_dates_keyboard,
    booking_times_keyboard,
    client_main_menu,
    owner_request_actions_keyboard,
)
from app.domain.booking import (
    MOSCOW_TZ,
    available_dates,
    available_slots_for_date,
    validate_title,
)


class BookingDomainTest(unittest.TestCase):
    def test_available_dates_skip_weekends(self) -> None:
        friday = datetime(2026, 7, 24, 9, 0, tzinfo=MOSCOW_TZ)
        dates = available_dates(friday)

        self.assertTrue(dates)
        self.assertNotIn(5, {item.weekday() for item in dates})
        self.assertNotIn(6, {item.weekday() for item in dates})

    def test_slots_respect_24_hour_notice(self) -> None:
        now = datetime(2026, 7, 24, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        slots_today = available_slots_for_date(now.date(), now=now)

        self.assertEqual(slots_today, [])

    def test_slots_are_hourly_inside_workday(self) -> None:
        now = datetime(2026, 7, 24, 9, 0, tzinfo=MOSCOW_TZ)
        selected_date = datetime(2026, 7, 27, 9, 0, tzinfo=MOSCOW_TZ).date()
        slots = available_slots_for_date(selected_date, now=now)

        self.assertEqual(slots[0].strftime("%H:%M"), "10:00")
        self.assertEqual(slots[-1].strftime("%H:%M"), "17:00")

    def test_title_validation(self) -> None:
        self.assertEqual(validate_title("  Консультация  "), "Консультация")
        self.assertIsNone(validate_title("ab"))
        self.assertIsNone(validate_title("x" * 101))

    def test_booking_dates_keyboard_contains_dates_and_cancel(self) -> None:
        dates = available_dates(datetime(2026, 7, 24, 9, 0, tzinfo=MOSCOW_TZ))[:3]
        keyboard = booking_dates_keyboard(dates)

        labels = [button.text for row in keyboard.keyboard for button in row]
        self.assertIn("27.07.2026", labels)
        self.assertIn("28.07.2026", labels)
        self.assertIn("Отмена", labels)

    def test_booking_times_keyboard_contains_slots_and_change_date(self) -> None:
        selected_date = datetime(2026, 7, 27, 9, 0, tzinfo=MOSCOW_TZ).date()
        slots = available_slots_for_date(
            selected_date,
            now=datetime(2026, 7, 24, 9, 0, tzinfo=MOSCOW_TZ),
        )
        keyboard = booking_times_keyboard(slots[:4])

        labels = [button.text for row in keyboard.keyboard for button in row]
        self.assertIn("10:00", labels)
        self.assertIn("11:00", labels)
        self.assertIn("Изменить дату", labels)
        self.assertIn("Отмена", labels)

    def test_booking_confirmation_keyboard_contains_main_actions(self) -> None:
        keyboard = booking_confirmation_keyboard()
        labels = [button.text for row in keyboard.keyboard for button in row]

        self.assertIn("Отправить заявку", labels)
        self.assertIn("Изменить дату", labels)
        self.assertIn("Изменить время", labels)
        self.assertIn("Изменить название", labels)
        self.assertIn("Изменить комментарий", labels)
        self.assertIn("Отмена", labels)

    def test_booking_comment_keyboard_contains_skip_option(self) -> None:
        keyboard = booking_comment_keyboard()
        labels = [button.text for row in keyboard.keyboard for button in row]

        self.assertIn("Без комментария", labels)
        self.assertIn("Отмена", labels)

    def test_client_main_menu_does_not_contain_profile_or_support(self) -> None:
        keyboard = client_main_menu()
        labels = [button.text for row in keyboard.keyboard for button in row]

        self.assertIn("Записаться на встречу", labels)
        self.assertIn("Мои заявки", labels)
        self.assertIn("Мои встречи", labels)
        self.assertNotIn("Мой профиль", labels)
        self.assertNotIn("Поддержка", labels)

    def test_owner_request_actions_keyboard_contains_inline_buttons(self) -> None:
        keyboard = owner_request_actions_keyboard(5)
        buttons = [button for row in keyboard.inline_keyboard for button in row]

        self.assertEqual(buttons[0].text, "Согласовать")
        self.assertEqual(buttons[0].callback_data, "approve_request:5:1")
        self.assertEqual(buttons[1].text, "Отклонить")
        self.assertEqual(buttons[1].callback_data, "reject_request:5:1")


if __name__ == "__main__":
    unittest.main()
