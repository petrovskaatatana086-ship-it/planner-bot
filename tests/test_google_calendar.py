import asyncio
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.db.repositories import (
    approve_booking_request,
    approve_meeting_reschedule,
    cancel_meeting,
    complete_client_registration,
    create_booking_request_from_draft,
    ensure_user_stub,
    reject_meeting_reschedule,
    request_meeting_reschedule,
    reschedule_meeting,
    update_meeting_google_data,
)
from app.db.session import create_schema, make_sessionmaker
from app.domain.booking import MOSCOW_TZ
from app.integrations.google_calendar import GoogleBusyPeriod, busy_periods_to_slot_starts


class GoogleCalendarTest(unittest.TestCase):
    def test_busy_periods_block_overlapping_slots(self) -> None:
        slots = [
            datetime(2026, 7, 27, 10, 0, tzinfo=MOSCOW_TZ),
            datetime(2026, 7, 27, 11, 0, tzinfo=MOSCOW_TZ),
            datetime(2026, 7, 27, 12, 0, tzinfo=MOSCOW_TZ),
        ]
        busy = [
            GoogleBusyPeriod(
                start=datetime(2026, 7, 27, 10, 30, tzinfo=MOSCOW_TZ),
                end=datetime(2026, 7, 27, 11, 30, tzinfo=MOSCOW_TZ),
            )
        ]

        blocked = busy_periods_to_slot_starts(busy, slots, duration_minutes=60)

        self.assertEqual(blocked, {slots[0], slots[1]})

    def test_google_event_data_is_saved_to_meeting(self) -> None:
        async def run() -> None:
            db_path = Path(".test_google_event.db")
            if db_path.exists():
                db_path.unlink()
            try:
                settings = Settings(
                    BOT_TOKEN="123:abc",
                    OWNER_TELEGRAM_ID=777,
                    DATABASE_URL="sqlite+aiosqlite:///./.test_google_event.db",
                )
                await create_schema(settings)
                session_factory = make_sessionmaker(settings)

                async with session_factory() as session:
                    client = await ensure_user_stub(
                        session,
                        telegram_id=100,
                        owner_telegram_id=777,
                        full_name="Client",
                        username=None,
                    )
                    await complete_client_registration(
                        session,
                        user=client,
                        full_name="Client",
                        email="client@example.com",
                    )
                    request = await create_booking_request_from_draft(
                        session,
                        client_id=client.id,
                        selected_start_local=datetime(2026, 7, 27, 10, 0, tzinfo=MOSCOW_TZ),
                        title="Consultation",
                        comment=None,
                    )
                    meeting = await approve_booking_request(
                        session,
                        request=request,
                        actor_user_id=client.id,
                    )
                    await update_meeting_google_data(
                        session,
                        meeting=meeting,
                        google_event_id="event-1",
                        google_meet_url="https://meet.google.com/abc-defg-hij",
                    )

                    self.assertEqual(meeting.google_event_id, "event-1")
                    self.assertEqual(meeting.google_meet_url, "https://meet.google.com/abc-defg-hij")
            finally:
                if db_path.exists():
                    db_path.unlink()

        asyncio.run(run())

    def test_meeting_can_be_rescheduled_and_cancelled_locally(self) -> None:
        async def run() -> None:
            db_path = Path(".test_meeting_lifecycle.db")
            if db_path.exists():
                db_path.unlink()
            try:
                settings = Settings(
                    BOT_TOKEN="123:abc",
                    OWNER_TELEGRAM_ID=777,
                    DATABASE_URL="sqlite+aiosqlite:///./.test_meeting_lifecycle.db",
                )
                await create_schema(settings)
                session_factory = make_sessionmaker(settings)

                async with session_factory() as session:
                    client = await ensure_user_stub(
                        session,
                        telegram_id=100,
                        owner_telegram_id=777,
                        full_name="Client",
                        username=None,
                    )
                    request = await create_booking_request_from_draft(
                        session,
                        client_id=client.id,
                        selected_start_local=datetime(2026, 7, 27, 10, 0, tzinfo=MOSCOW_TZ),
                        title="Consultation",
                        comment=None,
                    )
                    meeting = await approve_booking_request(
                        session,
                        request=request,
                        actor_user_id=client.id,
                    )
                    new_start = datetime(2026, 7, 28, 10, 0, tzinfo=MOSCOW_TZ)
                    await reschedule_meeting(
                        session,
                        meeting=meeting,
                        start_at=new_start,
                        end_at=new_start + timedelta(hours=1),
                        actor_user_id=client.id,
                    )
                    self.assertEqual(meeting.status, "rescheduled")
                    self.assertEqual(meeting.start_at, new_start)

                    await cancel_meeting(
                        session,
                        meeting=meeting,
                        actor_user_id=client.id,
                        reason="Schedule changed",
                        status="cancelled_by_client",
                    )
                    self.assertEqual(meeting.status, "cancelled_by_client")
            finally:
                if db_path.exists():
                    db_path.unlink()

        asyncio.run(run())

    def test_client_reschedule_request_can_be_approved_and_rejected(self) -> None:
        async def run() -> None:
            db_path = Path(".test_client_reschedule.db")
            if db_path.exists():
                db_path.unlink()
            try:
                settings = Settings(
                    BOT_TOKEN="123:abc",
                    OWNER_TELEGRAM_ID=777,
                    DATABASE_URL="sqlite+aiosqlite:///./.test_client_reschedule.db",
                )
                await create_schema(settings)
                session_factory = make_sessionmaker(settings)

                async with session_factory() as session:
                    client = await ensure_user_stub(
                        session,
                        telegram_id=100,
                        owner_telegram_id=777,
                        full_name="Client",
                        username=None,
                    )
                    request = await create_booking_request_from_draft(
                        session,
                        client_id=client.id,
                        selected_start_local=datetime(2026, 7, 27, 10, 0, tzinfo=MOSCOW_TZ),
                        title="Consultation",
                        comment=None,
                    )
                    meeting = await approve_booking_request(
                        session,
                        request=request,
                        actor_user_id=client.id,
                    )
                    proposed_start = datetime(2026, 7, 28, 10, 0, tzinfo=MOSCOW_TZ)
                    await request_meeting_reschedule(
                        session,
                        meeting=meeting,
                        proposed_start_at=proposed_start,
                        proposed_end_at=proposed_start + timedelta(hours=1),
                        comment="Prefer Tuesday",
                        actor_user_id=client.id,
                    )
                    self.assertEqual(meeting.status, "reschedule_pending")
                    self.assertEqual(meeting.reschedule_comment, "Prefer Tuesday")

                    await reject_meeting_reschedule(
                        session,
                        meeting=meeting,
                        actor_user_id=client.id,
                        reason="Busy",
                    )
                    self.assertEqual(meeting.status, "confirmed")
                    self.assertIsNone(meeting.proposed_start_at)

                    await request_meeting_reschedule(
                        session,
                        meeting=meeting,
                        proposed_start_at=proposed_start,
                        proposed_end_at=proposed_start + timedelta(hours=1),
                        comment=None,
                        actor_user_id=client.id,
                    )
                    await approve_meeting_reschedule(
                        session,
                        meeting=meeting,
                        actor_user_id=client.id,
                    )
                    self.assertEqual(meeting.status, "rescheduled")
                    self.assertEqual(meeting.start_at, proposed_start)
                    self.assertIsNone(meeting.proposed_start_at)
            finally:
                if db_path.exists():
                    db_path.unlink()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
