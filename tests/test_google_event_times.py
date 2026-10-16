import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.booking import MOSCOW_TZ
from app.integrations.google_calendar import (
    create_google_meeting_event,
    update_google_event_time,
)


class GoogleEventTimesTest(unittest.IsolatedAsyncioTestCase):
    async def check_payload(self, start_at: datetime, *, update: bool) -> None:
        response = MagicMock()
        response.status = 200
        response.json = AsyncMock(return_value={"id": "event-1"})
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = response
        session.patch.return_value = response

        with patch("app.integrations.google_calendar.ClientSession", return_value=session):
            if update:
                await update_google_event_time(
                    "test-token", calendar_id="primary", event_id="event-1",
                    start_at=start_at, end_at=start_at + timedelta(hours=1),
                )
                payload = session.patch.call_args.kwargs["json"]
            else:
                await create_google_meeting_event(
                    "test-token", calendar_id="primary", title="Meeting", description=None,
                    start_at=start_at, end_at=start_at + timedelta(hours=1),
                    attendee_email="client@example.com", request_id=10,
                )
                payload = session.post.call_args.kwargs["json"]

        self.assertEqual(payload["start"], {
            "dateTime": "2026-10-19T10:00:00+03:00", "timeZone": "Europe/Moscow",
        })
        self.assertEqual(payload["end"], {
            "dateTime": "2026-10-19T11:00:00+03:00", "timeZone": "Europe/Moscow",
        })

    async def test_create_interprets_naive_sqlite_timestamp_as_utc(self) -> None:
        # SQLite strips the UTC offset from stored DateTime(timezone=True) values.
        await self.check_payload(datetime(2026, 10, 19, 7), update=False)

    async def test_reschedule_interprets_naive_sqlite_timestamp_as_utc(self) -> None:
        await self.check_payload(datetime(2026, 10, 19, 7), update=True)

    async def test_aware_timestamps_keep_the_same_instant(self) -> None:
        for start in [
            datetime(2026, 10, 19, 7, tzinfo=timezone.utc),
            datetime(2026, 10, 19, 10, tzinfo=MOSCOW_TZ),
        ]:
            for update in [False, True]:
                with self.subTest(start=start, update=update):
                    await self.check_payload(start, update=update)
