import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

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


if __name__ == "__main__":
    unittest.main()

