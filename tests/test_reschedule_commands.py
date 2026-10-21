"""Shared reschedule/reject/cancel commands and isolated Google event-list tests."""
import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.base import Base
from app.db.models import (BookingRequest, CalendarSync, Meeting, OperationResult, OutboxEvent,
                           OwnerProfile, RescheduleRequest, User)
from app.db.session import make_engine
from app.domain.intervals import Interval, utc
from app.integrations.google_events import GoogleEventGateway, GoogleFailure
from app.services.availability import GoogleAvailability
from app.services.booking import BookingCommands, BookingError
from app.services.booking_deliveries import BookingDeliveries
from app.services.calendar_sync import CalendarDeliveries
from app.services.outbox import OutboxWorker
from test_calendar_sync import FakeCalendar


class RescheduleCommandsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path.cwd()/f".test_reschedule_{uuid.uuid4().hex}.db"
        self.settings = Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
            DATABASE_URL=f"sqlite+aiosqlite:///{self.path.as_posix()}")
        self.engines = [make_engine(self.settings), make_engine(self.settings)]
        self.sessions = [async_sessionmaker(e, expire_on_commit=False) for e in self.engines]
        async with self.engines[0].begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.now = datetime(2026, 10, 23, 7, tzinfo=timezone.utc)
        self.start = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
        self.target = self.start+timedelta(days=1)
        async with self.sessions[0]() as session:
            owner = User(telegram_id=777, role="owner", full_name="Owner")
            clients = [User(telegram_id=x, role="client", full_name="Synthetic", email=f"test{x}@example.com",
                            consent_at=self.now) for x in (101, 102)]
            session.add_all([owner, *clients])
            await session.flush()
            self.owner_id = owner.id
            self.clients = [c.id for c in clients]
            session.add(OwnerProfile(user_id=owner.id, default_duration=60))
            request = BookingRequest(client_id=clients[0].id, title="Synthetic", status="approved", version=2,
                start_at=self.start, end_at=self.start+timedelta(minutes=90))
            session.add(request)
            await session.flush()
            meeting = Meeting(booking_request_id=request.id, client_id=clients[0].id, title=request.title,
                start_at=request.start_at, end_at=request.end_at, status="confirmed", version=1)
            session.add(meeting)
            await session.commit()
            self.meeting_id = meeting.id
        self.commands = [BookingCommands(s, owner_telegram_id=777, clock=lambda: self.now,
                                         calendar_environment="test") for s in self.sessions]

    async def asyncTearDown(self):
        for engine in self.engines:
            await engine.dispose()
        self.path.unlink(missing_ok=True)

    async def record(self, model, identifier=1):
        async with self.sessions[0]() as session:
            return await session.get(model, identifier)

    async def count(self, model):
        async with self.sessions[0]() as session:
            return await session.scalar(select(func.count()).select_from(model))

    def propose(self, *, index=0, version=1, start=None, key=None, client=0):
        return self.commands[index].request_reschedule(actor_id=self.clients[client],
            command_id=key or uuid.uuid4().hex, meeting_id=self.meeting_id, expected_version=version,
            start_at=start or self.target, settings_version=1, comment="Synthetic proposal")

    def approve(self, proposal, *, index=0, key=None):
        return self.commands[index].approve_reschedule(actor_id=self.owner_id,
            command_id=key or uuid.uuid4().hex, meeting_id=self.meeting_id,
            expected_version=proposal["version"], reschedule_request_id=proposal["reschedule_request_id"])

    def reject(self, proposal, *, index=0):
        return self.commands[index].reject_reschedule(actor_id=self.owner_id,
            command_id=uuid.uuid4().hex, meeting_id=self.meeting_id, expected_version=proposal["version"],
            reschedule_request_id=proposal["reschedule_request_id"], reason="Synthetic rejection")

    def cancel(self, *, version=1, owner=False, index=0, key=None):
        return self.commands[index].cancel_meeting(actor_id=self.owner_id if owner else self.clients[0],
            command_id=key or uuid.uuid4().hex, meeting_id=self.meeting_id, expected_version=version,
            reason="Synthetic cancellation")

    async def test_proposal_keeps_current_time_and_duration_then_approval_moves(self):
        proposal = await self.propose()
        meeting = await self.record(Meeting)
        self.assertEqual(utc(meeting.start_at), self.start)
        history = await self.record(RescheduleRequest)
        self.assertEqual(utc(history.proposed_end_at)-utc(history.proposed_start_at), timedelta(minutes=90))
        result = await self.approve(proposal)
        meeting = await self.record(Meeting)
        self.assertEqual(result["status"], "rescheduled")
        self.assertEqual(utc(meeting.start_at), self.target)
        self.assertEqual(utc(meeting.end_at)-utc(meeting.start_at), timedelta(minutes=90))
        self.assertIsNone(meeting.proposed_start_at)
        self.assertEqual((await self.record(RescheduleRequest)).status, "approved")
        self.assertEqual((await self.record(BookingRequest)).status, "approved")

    async def test_rejection_restores_rescheduled_status_and_reason(self):
        async with self.sessions[0]() as session:
            meeting = await session.get(Meeting, 1)
            meeting.status = "rescheduled"
            await session.commit()
        proposal = await self.propose()
        await self.reject(proposal)
        self.assertEqual((await self.record(Meeting)).status, "rescheduled")
        history = await self.record(RescheduleRequest)
        self.assertEqual(history.reason, "Synthetic rejection")
        self.assertEqual(history.status, "rejected")
        self.assertEqual(utc((await self.record(Meeting)).start_at), self.start)

    async def test_new_slot_not_reserved_but_approval_rechecks_conflict(self):
        proposal = await self.propose()
        await self.commands[1].create_request(actor_id=self.clients[1], command_id=uuid.uuid4().hex,
            start_at=self.target, title="Competitor", settings_version=1)
        with self.assertRaises(BookingError) as error:
            await self.approve(proposal)
        self.assertEqual(error.exception.code, "SLOT_UNAVAILABLE")
        self.assertEqual((await self.record(Meeting)).status, "reschedule_pending")
        self.assertEqual((await self.record(RescheduleRequest)).status, "pending")

    async def test_proposal_and_approval_replay_do_not_duplicate_history_or_notifications(self):
        key = uuid.uuid4().hex
        proposal = await self.propose(key=key)
        self.assertEqual(proposal, await self.propose(index=1, key=key))
        approve_key = uuid.uuid4().hex
        result = await self.approve(proposal, key=approve_key)
        self.assertEqual(result, await self.approve(proposal, index=1, key=approve_key))
        self.assertEqual(await self.count(RescheduleRequest), 1)
        self.assertEqual(await self.count(OperationResult), 2)
        self.assertEqual(await self.count(OutboxEvent), 4)

    async def test_concurrent_proposals_have_one_winner(self):
        results = await asyncio.gather(self.propose(), self.propose(index=1), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(await self.count(RescheduleRequest), 1)

    async def test_cancel_against_approval_has_one_final_decision(self):
        proposal = await self.propose()
        results = await asyncio.gather(self.approve(proposal), self.cancel(version=2, index=1), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn((await self.record(Meeting)).status, ("rescheduled", "cancelled_by_client"))
        self.assertIn((await self.record(RescheduleRequest)).status, ("approved", "cancelled"))
        self.assertEqual((await self.record(Meeting)).version, 3)

    async def test_cancel_closes_pending_and_releases_old_slot(self):
        await self.propose()
        await self.cancel(version=2)
        meeting = await self.record(Meeting)
        self.assertEqual(meeting.cancellation_reason, "Synthetic cancellation")
        self.assertIsNone(meeting.proposed_start_at)
        self.assertEqual((await self.record(RescheduleRequest)).status, "cancelled")
        await self.commands[1].create_request(actor_id=self.clients[1], command_id=uuid.uuid4().hex,
            start_at=self.start, title="Now free", settings_version=1)
        self.assertEqual((await self.record(BookingRequest)).status, "approved")

    async def test_client_cancellation_notifies_owner_and_replay_does_not_duplicate(self):
        key = uuid.uuid4().hex
        first = await self.cancel(key=key)
        self.assertEqual(first, await self.cancel(index=1, key=key))
        bot = AsyncMock()
        adapter = BookingDeliveries(self.sessions[0], settings=self.settings, bot=bot, clock=lambda: self.now)
        worker = OutboxWorker(self.sessions[0], adapter.handlers(), clock=lambda: self.now)
        for _ in range(5):
            if not await worker.run_once():
                break
        self.assertEqual({call.args[0] for call in bot.send_message.await_args_list}, {777, 101})
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_approve_against_reject_request_has_one_winner(self):
        async with self.sessions[0]() as session:
            row = BookingRequest(client_id=self.clients[1], title="Pending", status="pending", version=1,
                start_at=self.target, end_at=self.target+timedelta(hours=1))
            session.add(row)
            await session.commit()
            identifier = row.id
        results = await asyncio.gather(
            self.commands[0].approve_request(actor_id=self.owner_id, command_id=uuid.uuid4().hex,
                request_id=identifier, expected_version=1),
            self.commands[1].reject_request(actor_id=self.owner_id, command_id=uuid.uuid4().hex,
                request_id=identifier, expected_version=1, reason="Unavailable"), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn((await self.record(BookingRequest, identifier)).status, ("approved", "rejected"))

    async def test_owner_direct_move_supersedes_pending_and_preserves_history(self):
        await self.propose()
        await self.commands[0].reschedule_by_owner(actor_id=self.owner_id, command_id=uuid.uuid4().hex,
            meeting_id=1, expected_version=2, start_at=self.target+timedelta(days=1), settings_version=1,
            reason="Owner proposal")
        self.assertEqual((await self.record(RescheduleRequest, 1)).status, "superseded")
        self.assertEqual((await self.record(RescheduleRequest, 2)).status, "approved")
        self.assertEqual((await self.record(Meeting)).status, "rescheduled")

    async def test_foreign_client_and_same_time_are_rejected(self):
        with self.assertRaises(BookingError) as error:
            await self.propose(client=1)
        self.assertEqual(error.exception.code, "NOT_FOUND")
        with self.assertRaises(BookingError) as error:
            await self.propose(start=self.start)
        self.assertEqual(error.exception.code, "VALIDATION_FAILED")

    async def test_cancel_notice_exact_boundary_and_owner_override(self):
        self.now = self.start-timedelta(hours=24)+timedelta(microseconds=1)
        with self.assertRaises(BookingError) as error:
            await self.cancel()
        self.assertEqual(error.exception.code, "NOTICE_REQUIRED")
        await self.cancel(owner=True)
        self.assertEqual((await self.record(Meeting)).status, "cancelled_by_owner")

    async def test_exact_cancel_boundary_allowed(self):
        self.now = self.start-timedelta(hours=24)
        await self.cancel()
        self.assertEqual((await self.record(Meeting)).status, "cancelled_by_client")

    async def test_disabled_booking_blocks_proposal_but_not_cancel(self):
        async with self.sessions[0]() as session:
            profile = await session.scalar(select(OwnerProfile))
            profile.booking_enabled = False
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.propose()
        self.assertEqual(error.exception.code, "BOOKING_DISABLED")
        await self.cancel()

    async def test_pending_unique_index_is_database_enforced(self):
        await self.propose()
        async with self.sessions[0]() as session:
            session.add(RescheduleRequest(meeting_id=1, requested_by_id=self.clients[0], status="pending",
                proposed_start_at=self.target, proposed_end_at=self.target+timedelta(minutes=90)))
            with self.assertRaises(IntegrityError):
                await session.commit()
            await session.rollback()

    async def test_legacy_unknown_previous_state_requires_review_on_rejection(self):
        proposal = await self.propose()
        async with self.sessions[0]() as session:
            row = await session.get(RescheduleRequest, 1)
            row.previous_status = None
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.reject(proposal)
        self.assertEqual(error.exception.code, "LEGACY_RESCHEDULE_REVIEW_REQUIRED")
        self.assertEqual((await self.record(RescheduleRequest)).status, "pending")

    async def test_decline_pending_request_needs_reason_and_does_not_touch_meeting(self):
        async with self.sessions[0]() as session:
            request = BookingRequest(client_id=self.clients[1], title="Pending", status="pending", version=1,
                start_at=self.target, end_at=self.target+timedelta(hours=1), reserved_until=self.now+timedelta(minutes=30))
            session.add(request)
            await session.commit()
            identifier = request.id
        with self.assertRaises(BookingError):
            await self.commands[0].reject_request(actor_id=self.owner_id, command_id=uuid.uuid4().hex,
                request_id=identifier, expected_version=1, reason="  ")
        await self.commands[0].reject_request(actor_id=self.owner_id, command_id=uuid.uuid4().hex,
            request_id=identifier, expected_version=1, reason="Unavailable")
        self.assertEqual((await self.record(BookingRequest, identifier)).rejection_reason, "Unavailable")
        self.assertEqual(await self.count(Meeting), 1)

    async def connect(self):
        async with self.sessions[0]() as session:
            profile = await session.scalar(select(OwnerProfile))
            profile.calendar_id = "synthetic-calendar"
            profile.google_status = "active"
            meeting = await session.get(Meeting, 1)
            meeting.google_event_id = "1"*32
            session.add(CalendarSync(meeting_id=1, environment="test", calendar_id=profile.calendar_id,
                deterministic_event_id=meeting.google_event_id, conference_request_id="2"*32,
                desired_version=1, applied_version=1, state="synced",
                applied_start_at=meeting.start_at, applied_end_at=meeting.end_at))
            await session.commit()
        self.busy = AsyncMock(return_value=())
        gateway = type("AvailabilityGateway", (), {})()
        gateway.busy_excluding = self.busy
        for command in self.commands:
            command.availability = GoogleAvailability(gateway, clock=lambda: self.now)
        events = FakeCalendar()
        events.events["1"*32] = {"id": "1"*32, "etag": '"old"', "status": "confirmed",
            "start": {"dateTime": self.start.isoformat()}, "end": {"dateTime": (self.start+timedelta(minutes=90)).isoformat()},
            "extendedProperties": {"private": {"meetly_environment": "test", "meetly_meeting_id": "1", "meetly_version": "1"}},
            "hangoutLink": "https://meet.google.com/abc-defg-hij"}
        self.events = events
        self.bot = AsyncMock()
        calendar = CalendarDeliveries(self.sessions[0], gateway=events, owner_telegram_id=777,
                                      environment="test", clock=lambda: self.now)
        notices = BookingDeliveries(self.sessions[0], settings=self.settings, bot=self.bot,
                                    calendar_environment="test", clock=lambda: self.now)
        self.worker = OutboxWorker(self.sessions[0], {**calendar.handlers(), **notices.handlers()}, clock=lambda: self.now)

    async def drain(self):
        for _ in range(20):
            if not await self.worker.run_once():
                return
        self.fail("worker did not drain")

    async def test_google_exact_event_exclusion_and_sync_to_client(self):
        await self.connect()
        proposal = await self.propose()
        self.assertEqual(self.busy.await_args.args[3], "1"*32)
        await self.approve(proposal)
        await self.drain()
        self.assertEqual(self.events.patches, 1)
        self.assertEqual((await self.record(CalendarSync)).applied_version, 3)
        self.bot.send_message.assert_awaited_once()
        self.assertIn("27.10.2026", self.bot.send_message.await_args.args[1])
        self.assertIn("https://meet.google.com/", self.bot.send_message.await_args.args[1])

    async def test_google_foreign_overlap_not_ignored_when_own_id_excluded(self):
        await self.connect()
        self.busy.return_value = (Interval(self.target, self.target+timedelta(hours=1)),)
        with self.assertRaises(BookingError) as error:
            await self.propose()
        self.assertEqual(error.exception.code, "SLOT_UNAVAILABLE")
        self.assertEqual(await self.count(RescheduleRequest), 0)

    async def test_cancel_commits_with_unavailable_google_and_skips_old_failed_create(self):
        await self.connect()
        async with self.sessions[0]() as session:
            profile = await session.scalar(select(OwnerProfile))
            profile.google_status = "error"
            session.add(OutboxEvent(event_id=uuid.uuid4().hex, entity_id=1, entity_version=1,
                channel="google", kind="meeting.sync", state="failed", attempts=6,
                next_attempt_at=self.now, created_at=self.now))
            await session.commit()
        await self.cancel()
        self.assertEqual((await self.record(Meeting)).status, "cancelled_by_client")
        self.assertEqual((await self.record(OutboxEvent, 1)).state, "superseded")
        self.busy.assert_not_awaited()

    async def test_metadata_only_proposal_does_not_send_google_time_update(self):
        await self.connect()
        original = self.events.patch
        self.events.patch = AsyncMock(wraps=original)
        await self.propose()
        await self.drain()
        self.assertFalse(self.events.patch.await_args.kwargs["notify"])
        self.assertEqual(self.bot.send_message.await_args.args[0], 777)


class EventsListAvailabilityTest(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_excludes_only_exact_id_and_expands_recurring(self):
        start = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
        def event(identifier):
            return {"id": identifier, "start": {"dateTime": start.isoformat()},
                    "end": {"dateTime": (start+timedelta(hours=1)).isoformat()}}
        requester = AsyncMock(side_effect=[
            (200, {"accessRole": "owner", "timeZone": "Europe/Moscow", "items": [event("own")], "nextPageToken": "page2"}, {}),
            (200, {"accessRole": "owner", "timeZone": "Europe/Moscow", "items": [
                {**event("foreign_instance"), "recurringEventId": "series"},
                {**event("transparent"), "transparency": "transparent"},
                {"id": "deleted", "status": "cancelled"}]}, {})])
        gateway = GoogleEventGateway(AsyncMock(return_value="token"), requester=requester)
        periods = await gateway.busy_excluding("calendar", start, start+timedelta(hours=2), "own")
        self.assertEqual(periods, (Interval(start, start+timedelta(hours=1)),))
        self.assertEqual(requester.await_args.kwargs["params"]["singleEvents"], "true")
        self.assertEqual(requester.await_args.kwargs["params"]["pageToken"], "page2")

    async def test_all_day_uses_calendar_timezone(self):
        requester = AsyncMock(return_value=(200, {"accessRole": "reader", "timeZone": "America/New_York",
            "items": [{"id": "all-day", "start": {"date": "2026-10-26"}, "end": {"date": "2026-10-27"}}]}, {}))
        gateway = GoogleEventGateway(AsyncMock(return_value="token"), requester=requester)
        start = datetime(2026, 10, 26, tzinfo=timezone.utc)
        periods = await gateway.busy_excluding("calendar", start, start+timedelta(days=2), "own")
        self.assertEqual(periods[0].start.hour, 4)
        self.assertEqual(periods[0].end-periods[0].start, timedelta(days=1))

    async def test_incomplete_permissions_and_repeated_tokens_fail_closed(self):
        start = datetime(2026, 10, 26, tzinfo=timezone.utc)
        for body in ({"accessRole": "freeBusyReader", "items": []},
                     {"accessRole": "owner"},
                     {"accessRole": "owner", "items": [], "nextPageToken": "same"}):
            gateway = GoogleEventGateway(AsyncMock(return_value="token"), requester=AsyncMock(return_value=(200, body, {})))
            with self.subTest(body=body), self.assertRaises(GoogleFailure):
                await gateway.busy_excluding("calendar", start, start+timedelta(days=1), "own")
