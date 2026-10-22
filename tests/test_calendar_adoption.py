"""Legacy adoption: fake Google, SQLite intent fencing and real booking/outbox guards."""
import copy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.base import Base
from app.db.models import AuditLog, BookingRequest, CalendarSync, Meeting, OutboxEvent, OwnerProfile, User
from app.db.session import make_engine
from app.integrations.google_events import GoogleFailure
from app.services.booking import BookingCommands, BookingError
from app.services.bot_booking import BotBooking
from app.services.calendar_adoption import AdoptionPlan, CalendarAdoption
from app.services.calendar_sync import CalendarDeliveries, enqueue_calendar_sync
from app.services.outbox import DeliveryError, OutboxWorker
from tests.test_calendar_sync import FakeCalendar


class AdoptionCalendar(FakeCalendar):
    async def patch(self, calendar, identifier, body, etag, *, notify=True):
        self.patches += 1
        event = self.events[identifier]
        if event["etag"] != etag:
            raise GoogleFailure(412)
        for key, value in body.items():
            if key != "extendedProperties":
                event[key] = copy.deepcopy(value)
        for key, value in body.get("extendedProperties", {}).items():
            event.setdefault("extendedProperties", {}).setdefault(key, {}).update(copy.deepcopy(value))
        event["etag"] = '"adopted"'
        if self.lose_patch_reply:
            self.lose_patch_reply = False
            raise GoogleFailure(0)
        return copy.deepcopy(event)


class CalendarAdoptionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path.cwd()/f".test_adoption_{uuid.uuid4().hex}.db"
        settings = Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
            DATABASE_URL=f"sqlite+aiosqlite:///{self.path.as_posix()}")
        self.engines = [make_engine(settings), make_engine(settings)]
        self.sessions = [async_sessionmaker(e, expire_on_commit=False) for e in self.engines]
        async with self.engines[0].begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.now = datetime(2026, 10, 22, 7, tzinfo=timezone.utc)
        async with self.sessions[0]() as session:
            owner = User(telegram_id=777, full_name="Owner", role="owner", status="active")
            client = User(telegram_id=101, full_name="Synthetic", email="test@example.com", role="client", status="active")
            session.add_all([owner, client])
            await session.flush()
            self.owner_id, self.client_id = owner.id, client.id
            profile = OwnerProfile(user_id=owner.id, calendar_id="synthetic-calendar", google_status="active")
            request = BookingRequest(client_id=client.id, title="Synthetic", status="approved",
                start_at=self.now+timedelta(days=4), end_at=self.now+timedelta(days=4, hours=1))
            session.add_all([profile, request])
            await session.flush()
            meeting = Meeting(booking_request_id=request.id, client_id=client.id, title=request.title,
                start_at=request.start_at, end_at=request.end_at, status="confirmed", google_event_id="legacy-event")
            session.add(meeting)
            await session.commit()
            self.meeting_id = meeting.id
            self.event = dict(id="legacy-event", status="confirmed", summary=meeting.title, etag='"legacy"',
                start=dict(dateTime=meeting.start_at.isoformat()), end=dict(dateTime=meeting.end_at.isoformat()),
                attendees=[dict(email=client.email, responseStatus="accepted")], description="Preserve this",
                hangoutLink="https://meet.google.com/abc-defg-hij", htmlLink="https://calendar.google.com/calendar/event?eid=test",
                extendedProperties=dict(private=dict(other_tool="preserve"), shared=dict(shared_tool="preserve")),
                conferenceData=dict(conferenceId="abc-defg-hij"))
        self.gateway = AdoptionCalendar()
        self.gateway.events["legacy-event"] = copy.deepcopy(self.event)
        self.service = CalendarAdoption(self.sessions[0], gateway=self.gateway, owner_telegram_id=777,
                                        environment="test", clock=lambda: self.now)

    async def asyncTearDown(self):
        for engine in self.engines:
            await engine.dispose()
        self.path.unlink(missing_ok=True)

    async def inspect(self):
        return await self.service.inspect(actor_id=self.owner_id, meeting_id=self.meeting_id)

    async def adopt(self, plan=None):
        return await self.service.adopt(actor_id=self.owner_id, plan=plan or await self.inspect())

    async def sync(self):
        async with self.sessions[0]() as session:
            return await session.get(CalendarSync, self.meeting_id)

    async def test_explicit_adoption_only_patches_metadata_without_notifying(self):
        plan = await self.inspect()
        self.assertIsNone(await self.sync())
        self.assertNotIn("test@example.com", str(asdict(plan)))
        with patch.object(self.gateway, "patch", wraps=self.gateway.patch) as patch_event:
            result = await self.adopt(plan)
        self.assertEqual(result["state"], "synced")
        self.assertEqual(set(patch_event.call_args.args[2]), {"extendedProperties"})
        self.assertFalse(patch_event.call_args.kwargs["notify"])
        adopted = self.gateway.events["legacy-event"]
        for field in self.event.keys()-{"etag", "extendedProperties"}:
            self.assertEqual(adopted[field], self.event[field])
        self.assertEqual(adopted["extendedProperties"]["private"]["other_tool"], "preserve")
        self.assertEqual(adopted["extendedProperties"]["shared"], self.event["extendedProperties"]["shared"])
        sync = await self.sync()
        self.assertEqual((sync.deterministic_event_id, sync.applied_version), ("legacy-event", 1))
        self.assertEqual((self.gateway.inserts, self.gateway.deletes), (0, 0))
        self.assertEqual(await self.adopt(plan), result)
        self.assertEqual(self.gateway.patches, 1)
        async with self.sessions[0]() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "calendar.legacy.adopted")), 1)
            self.assertEqual(await session.scalar(select(func.count()).select_from(OutboxEvent)), 0)

    async def test_timeout_after_remote_patch_recovers_without_second_patch(self):
        plan = await self.inspect()
        self.gateway.lose_patch_reply = True
        with self.assertRaises(GoogleFailure):
            await self.adopt(plan)
        self.assertEqual((await self.sync()).state, "adopting")
        await self.adopt(plan)
        self.assertEqual((await self.sync()).state, "synced")
        self.assertEqual(self.gateway.patches, 1)

    async def test_missing_removed_foreign_and_mismatched_events_are_never_adopted(self):
        invalids = [None, {**self.event, "status": "cancelled"},
            {**self.event, "summary": "Changed"}, {**self.event, "recurringEventId": "series"},
            {**self.event, "attendees": [dict(email="other@example.com")]},
            {**self.event, "start": dict(date="2026-10-26")},
            {**self.event, "hangoutLink": "https://untrusted.example/test"},
            {**self.event, "extendedProperties": dict(private=dict(meetly_environment="production"))}]
        for event in invalids:
            with self.subTest(event=event and set(event)):
                self.gateway.events["legacy-event"] = copy.deepcopy(event)
                with self.assertRaises(BookingError):
                    await self.inspect()
                self.assertIsNone(await self.sync())
        self.assertEqual(self.gateway.patches, 0)

    async def test_external_edit_between_inspection_and_apply_requires_new_review(self):
        plan = await self.inspect()
        self.gateway.events["legacy-event"]["etag"] = '"changed"'
        with self.assertRaises(BookingError) as error:
            await self.adopt(plan)
        self.assertEqual(error.exception.code, "LEGACY_ETAG_CHANGED")
        self.assertIsNone(await self.sync())

    async def test_etag_precondition_failure_keeps_intent_for_review(self):
        plan = await self.inspect()
        with patch.object(self.gateway, "patch", side_effect=GoogleFailure(412)):
            with self.assertRaises(GoogleFailure):
                await self.adopt(plan)
        self.assertEqual((await self.sync()).state, "adopting")
        self.assertNotIn("meetly_environment", self.gateway.events["legacy-event"]["extendedProperties"]["private"])

    async def test_silent_local_changes_and_wrong_environment_reject_old_plan(self):
        plan = await self.inspect()
        with self.assertRaises(BookingError):
            await self.adopt(replace(plan, environment="production"))
        async with self.sessions[1]() as session:
            (await session.get(Meeting, self.meeting_id)).title = "Changed without version"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.adopt(plan)
        self.assertEqual(error.exception.code, "STALE_VERSION")
        self.assertEqual(self.gateway.patches, 0)

    async def test_network_has_no_database_lock_and_profile_change_prevents_intent(self):
        plan = await self.inspect()
        original = self.gateway.get
        async def get(calendar, event):
            async with self.sessions[1]() as session:
                profile = await session.scalar(select(OwnerProfile))
                profile.version += 1
                await session.commit()
            return await original(calendar, event)
        self.gateway.get = get
        with self.assertRaises(BookingError) as error:
            await self.adopt(plan)
        self.assertEqual(error.exception.code, "STALE_VERSION")
        self.assertIsNone(await self.sync())

    async def test_access_checked_before_remote_and_before_replay(self):
        plan = await self.inspect()
        await self.adopt(plan)
        async with self.sessions[0]() as session:
            (await session.get(User, self.owner_id)).status = "blocked"
            (await session.get(User, self.client_id)).role = "owner"
            await session.commit()
        with patch.object(self.gateway, "get", wraps=self.gateway.get) as get:
            for actor, code in ((self.owner_id, "ACCOUNT_BLOCKED"), (self.client_id, "FORBIDDEN")):
                with self.assertRaises(BookingError) as error:
                    await self.service.adopt(actor_id=actor, plan=plan)
                self.assertEqual(error.exception.code, code)
            get.assert_not_awaited()

    async def test_cancelled_and_existing_deliveries_require_manual_review(self):
        async with self.sessions[0]() as session:
            meeting = await session.get(Meeting, self.meeting_id)
            meeting.status = "cancelled_by_owner"
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.inspect()
        self.assertEqual(error.exception.code, "LEGACY_MEETING_REVIEW_REQUIRED")
        async with self.sessions[0]() as session:
            (await session.get(Meeting, self.meeting_id)).status = "confirmed"
            session.add(OutboxEvent(event_id=uuid.uuid4().hex, channel="google", kind="meeting.sync",
                entity_id=self.meeting_id, entity_version=1, state="pending", attempts=0, next_attempt_at=self.now))
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.inspect()
        self.assertEqual(error.exception.code, "CALENDAR_DELIVERY_EXISTS")

    async def test_booking_cancellation_rolls_back_until_adoption_then_syncs_existing_event(self):
        commands = BookingCommands(self.sessions[0], owner_telegram_id=777, calendar_environment="test", clock=lambda: self.now)
        with self.assertRaises(BookingError) as error:
            await BotBooking(commands).execute("cancel_meeting", actor_id=self.owner_id, command_id="cancel",
                                               meeting_id=self.meeting_id, expected_version=1)
        self.assertEqual(error.exception.code, "CALENDAR_ADOPTION_REQUIRED")
        async with self.sessions[0]() as session:
            meeting = await session.get(Meeting, self.meeting_id)
            self.assertEqual((meeting.status, meeting.version), ("confirmed", 1))
        await self.adopt()
        await commands.cancel_meeting(actor_id=self.owner_id, command_id="cancel", meeting_id=self.meeting_id,
                                       expected_version=1)
        adapters = CalendarDeliveries(self.sessions[0], gateway=self.gateway, owner_telegram_id=777,
                                      environment="test", clock=lambda: self.now)
        worker = OutboxWorker(self.sessions[0], adapters.handlers(), clock=lambda: self.now)
        await worker.run_once()
        self.assertEqual((self.gateway.inserts, self.gateway.deletes), (0, 1))

    async def test_adopting_state_blocks_commands_and_worker_cannot_change_intent(self):
        plan = await self.inspect()
        self.gateway.lose_patch_reply = True
        with self.assertRaises(GoogleFailure):
            await self.adopt(plan)
        async with self.sessions[0]() as session:
            meeting = await session.get(Meeting, self.meeting_id)
            profile = await session.scalar(select(OwnerProfile))
            with self.assertRaises(DeliveryError) as error:
                await enqueue_calendar_sync(session, meeting=meeting, profile=profile, environment="test", now=self.now)
            self.assertEqual(error.exception.code, "CALENDAR_ADOPTION_REQUIRED")
            session.add(OutboxEvent(event_id=uuid.uuid4().hex, entity_id=meeting.id, entity_version=1,
                channel="google", kind="meeting.sync", state="pending", attempts=0, next_attempt_at=self.now))
            await session.commit()
        adapters = CalendarDeliveries(self.sessions[0], gateway=self.gateway, owner_telegram_id=777,
                                      environment="test", clock=lambda: self.now)
        await OutboxWorker(self.sessions[0], adapters.handlers(), clock=lambda: self.now).run_once()
        self.assertEqual((await self.sync()).state, "adopting")
        self.assertEqual((self.gateway.inserts, self.gateway.deletes), (0, 0))

    async def test_rescheduling_after_adoption_updates_the_same_event(self):
        await self.adopt()
        async with self.sessions[0]() as session:
            meeting = await session.get(Meeting, self.meeting_id)
            meeting.version += 1
            meeting.start_at += timedelta(days=1)
            meeting.end_at += timedelta(days=1)
            meeting.status = "rescheduled"
            profile = await session.scalar(select(OwnerProfile))
            await enqueue_calendar_sync(session, meeting=meeting, profile=profile, environment="test", now=self.now)
            await session.commit()
        adapters = CalendarDeliveries(self.sessions[0], gateway=self.gateway, owner_telegram_id=777,
                                      environment="test", clock=lambda: self.now)
        await OutboxWorker(self.sessions[0], adapters.handlers(), clock=lambda: self.now).run_once()
        self.assertEqual((await self.sync()).applied_version, 2)
        self.assertEqual(self.gateway.events["legacy-event"]["start"]["dateTime"],
                         (self.now+timedelta(days=5)).isoformat())
        self.assertEqual((self.gateway.inserts, self.gateway.deletes, self.gateway.patches), (0, 0, 2))

    async def test_shared_event_id_is_not_assigned_to_either_meeting(self):
        async with self.sessions[0]() as session:
            request = BookingRequest(client_id=self.client_id, title="Duplicate", status="approved",
                start_at=self.now, end_at=self.now+timedelta(hours=1))
            session.add(request)
            await session.flush()
            session.add(Meeting(booking_request_id=request.id, client_id=self.client_id, title="Duplicate",
                start_at=request.start_at, end_at=request.end_at, google_event_id="legacy-event", status="confirmed"))
            await session.commit()
        with self.assertRaises(BookingError) as error:
            await self.inspect()
        self.assertEqual(error.exception.code, "LEGACY_EVENT_SHARED")
        self.assertIsNone(await self.sync())

    async def test_old_unapplied_sync_cannot_recreate_missing_legacy_event(self):
        self.gateway.events.clear()
        async with self.sessions[0]() as session:
            session.add(CalendarSync(meeting_id=self.meeting_id, environment="test", calendar_id="synthetic-calendar",
                deterministic_event_id="legacy-event", conference_request_id=uuid.uuid4().hex,
                desired_version=1, applied_version=0, state="pending"))
            session.add(OutboxEvent(event_id=uuid.uuid4().hex, entity_id=self.meeting_id, entity_version=1,
                channel="google", kind="meeting.sync", state="pending", attempts=0, next_attempt_at=self.now))
            await session.commit()
        adapters = CalendarDeliveries(self.sessions[0], gateway=self.gateway, owner_telegram_id=777,
                                      environment="test", clock=lambda: self.now)
        await OutboxWorker(self.sessions[0], adapters.handlers(), clock=lambda: self.now).run_once()
        self.assertEqual((await self.sync()).last_error_code, "CALENDAR_ADOPTION_REQUIRED")
        self.assertEqual(self.gateway.inserts, 0)
