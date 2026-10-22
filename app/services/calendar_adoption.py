"""Explicit, resumable adoption of a stored legacy event. Never creates/deletes events."""
import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.db.models import AuditLog, BookingRequest, CalendarSync, Meeting, OutboxEvent, OwnerProfile, User
from app.db.transactions import run_sqlite_write
from app.domain.intervals import utc
from app.services.booking import BookingError
from app.services.calendar_sync import remote_time, safe_link
from app.services.outbox import DeliveryError


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class AdoptionPlan:
    meeting_id: int
    meeting_version: int
    profile_version: int
    environment: str
    calendar_hash: str
    event_hash: str
    content_hash: str
    etag: str

    def __post_init__(self):
        if (any(type(v) is not int or v < 1 for v in
                (self.meeting_id, self.meeting_version, self.profile_version))
                or not isinstance(self.environment, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.environment)
                or any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v)
                       for v in (self.calendar_hash, self.event_hash, self.content_hash))
                or not isinstance(self.etag, str) or not 1 <= len(self.etag) <= 1024
                or any(ord(c) < 32 for c in self.etag)):
            raise BookingError("INVALID_ADOPTION_PLAN")


@dataclass(frozen=True)
class LegacySnapshot:
    meeting_id: int
    version: int
    profile_version: int
    calendar: str
    event: str
    title: str
    email: str
    start: datetime
    end: datetime
    status: str
    intent: str | None
    state: str | None


class CalendarAdoption:
    def __init__(self, sessions, *, gateway, owner_telegram_id, environment, clock=None):
        if not isinstance(environment, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", environment):
            raise ValueError("Invalid calendar environment")
        self.sessions, self.gateway = sessions, gateway
        self.owner_telegram_id, self.environment = owner_telegram_id, environment
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def _load(self, session, actor_id, meeting_id):
        actor = await session.get(User, actor_id)
        if actor is None or actor.status != "active":
            raise BookingError("ACCOUNT_BLOCKED")
        if actor.telegram_id != self.owner_telegram_id:
            raise BookingError("FORBIDDEN")
        profile = await session.scalar(select(OwnerProfile).where(OwnerProfile.user_id == actor.id))
        if profile is None or not profile.calendar_id or profile.google_status != "active":
            raise BookingError("CALENDAR_UNAVAILABLE")
        meeting = await session.get(Meeting, meeting_id)
        if meeting is None:
            raise BookingError("RESOURCE_NOT_FOUND")
        if meeting.status not in ("confirmed", "rescheduled", "reschedule_pending"):
            raise BookingError("LEGACY_MEETING_REVIEW_REQUIRED")
        if not meeting.google_event_id:
            raise BookingError("LEGACY_EVENT_MISSING")
        if await session.scalar(select(Meeting.id).where(Meeting.id != meeting.id,
                Meeting.google_event_id == meeting.google_event_id).limit(1)) is not None:
            raise BookingError("LEGACY_EVENT_SHARED")
        client = await session.get(User, meeting.client_id)
        request = await session.get(BookingRequest, meeting.booking_request_id)
        if (client is None or not client.email or request is None or request.status != "approved"
                or request.client_id != meeting.client_id):
            raise BookingError("LEGACY_MEETING_REVIEW_REQUIRED")
        # Adoption is performed before runtime activation; never race an existing delivery.
        if await session.scalar(select(OutboxEvent.id).where(
                OutboxEvent.channel == "google", OutboxEvent.entity_id == meeting.id).limit(1)) is not None:
            raise BookingError("CALENDAR_DELIVERY_EXISTS")
        sync = await session.get(CalendarSync, meeting.id)
        if sync and (sync.environment != self.environment or sync.calendar_id != profile.calendar_id
                     or sync.deterministic_event_id != meeting.google_event_id
                     or sync.desired_version != meeting.version or sync.state not in ("adopting", "synced")):
            raise BookingError("CALENDAR_ADOPTION_CONFLICT")
        if sync and (utc(sync.applied_start_at) if sync.applied_start_at else None,
                     utc(sync.applied_end_at) if sync.applied_end_at else None) != (utc(meeting.start_at), utc(meeting.end_at)):
            raise BookingError("CALENDAR_ADOPTION_CONFLICT")
        return LegacySnapshot(meeting.id, meeting.version, profile.version, profile.calendar_id,
            meeting.google_event_id, meeting.title, client.email.lower(), utc(meeting.start_at), utc(meeting.end_at),
            meeting.status, sync.conference_request_id if sync else None, sync.state if sync else None)

    async def _snapshot(self, actor_id, meeting_id):
        async with self.sessions() as session:
            await session.execute(text("BEGIN"))
            return await self._load(session, actor_id, meeting_id)

    def _plan(self, snapshot, etag):
        return AdoptionPlan(snapshot.meeting_id, snapshot.version, snapshot.profile_version, self.environment,
            digest(snapshot.calendar), digest(snapshot.event),
            digest(json.dumps([snapshot.title, snapshot.email, snapshot.start.isoformat(),
                               snapshot.end.isoformat(), snapshot.status])), etag)

    def _tags(self, snapshot):
        return dict(meetly_environment=self.environment, meetly_meeting_id=str(snapshot.meeting_id),
                    meetly_version=str(snapshot.version), meetly_adoption_id=snapshot.intent)

    def _validate(self, snapshot, event):
        if not isinstance(event, dict) or event.get("id") != snapshot.event or event.get("status") != "confirmed":
            raise BookingError("LEGACY_EVENT_REVIEW_REQUIRED")
        if (event.get("recurrence") or event.get("recurringEventId") or event.get("originalStartTime")
                or event.get("eventType", "default") != "default" or event.get("attendeesOmitted")
                or event.get("transparency", "opaque") != "opaque" or event.get("locked")):
            raise BookingError("LEGACY_EVENT_REVIEW_REQUIRED")
        attendees = event.get("attendees")
        if (not isinstance(attendees, list) or len(attendees) != 1 or not isinstance(attendees[0], dict)
                or str(attendees[0].get("email", "")).lower() != snapshot.email
                or attendees[0].get("responseStatus") == "declined"):
            raise BookingError("LEGACY_EVENT_REVIEW_REQUIRED")
        try:
            if (remote_time(event, "start"), remote_time(event, "end"), event.get("summary")) != (
                    snapshot.start, snapshot.end, snapshot.title):
                raise BookingError("LEGACY_EVENT_REVIEW_REQUIRED")
            safe_link(event.get("htmlLink"), {"calendar.google.com", "www.google.com"})
            if not safe_link(event.get("hangoutLink"), {"meet.google.com"}):
                raise BookingError("LEGACY_EVENT_REVIEW_REQUIRED")
        except (DeliveryError, ValueError):
            raise BookingError("LEGACY_EVENT_REVIEW_REQUIRED") from None
        properties = event.get("extendedProperties", {})
        private = properties.get("private", {}) if isinstance(properties, dict) else None
        if not isinstance(private, dict):
            raise BookingError("LEGACY_EVENT_REVIEW_REQUIRED")
        reserved = {key: value for key, value in private.items() if key.startswith("meetly_")}
        owned = bool(snapshot.intent and reserved == self._tags(snapshot))
        if reserved and not owned:
            raise BookingError("CALENDAR_OWNERSHIP_CONFLICT")
        if not isinstance(event.get("etag"), str) or not event["etag"]:
            raise BookingError("LEGACY_EVENT_REVIEW_REQUIRED")
        return private, owned

    async def inspect(self, *, actor_id, meeting_id):
        snapshot = await self._snapshot(actor_id, meeting_id)
        event = await self.gateway.get(snapshot.calendar, snapshot.event)
        self._validate(snapshot, event)
        # Network reads never make an outdated plan reviewable as current.
        if await self._snapshot(actor_id, meeting_id) != snapshot:
            raise BookingError("STALE_VERSION")
        return self._plan(snapshot, event["etag"])

    async def adopt(self, *, actor_id, plan):
        if not isinstance(plan, AdoptionPlan) or plan.environment != self.environment:
            raise BookingError("INVALID_ADOPTION_PLAN")
        snapshot = await self._snapshot(actor_id, plan.meeting_id)
        if self._plan(snapshot, plan.etag) != plan:
            raise BookingError("STALE_VERSION")
        event = await self.gateway.get(snapshot.calendar, snapshot.event)
        private, owned = self._validate(snapshot, event)
        if not owned and event["etag"] != plan.etag:
            raise BookingError("LEGACY_ETAG_CHANGED")

        async def reserve(session):
            current = await self._load(session, actor_id, plan.meeting_id)
            if current != snapshot:
                raise BookingError("STALE_VERSION")
            if current.intent:
                return current
            intent = uuid.uuid4().hex
            session.add(CalendarSync(meeting_id=current.meeting_id, environment=self.environment,
                calendar_id=current.calendar, deterministic_event_id=current.event, conference_request_id=intent,
                desired_version=current.version, applied_version=0, applied_start_at=current.start,
                applied_end_at=current.end, state="adopting"))
            session.add(AuditLog(actor_user_id=actor_id, action="calendar.legacy.adoption_started",
                entity_type="meeting", entity_id=str(current.meeting_id), created_at=utc(self.clock())))
            return LegacySnapshot(**{**asdict(current), "intent": intent, "state": "adopting"})
        snapshot = await run_sqlite_write(self.sessions, reserve)
        if not owned:
            event = await self.gateway.patch(snapshot.calendar, snapshot.event,
                {"extendedProperties": {"private": {**private, **self._tags(snapshot)}}}, event["etag"], notify=False)
        _, owned = self._validate(snapshot, event)
        if not owned:
            raise BookingError("CALENDAR_OWNERSHIP_CONFLICT")

        async def finish(session):
            current = await self._load(session, actor_id, plan.meeting_id)
            if current != snapshot:
                raise BookingError("STALE_VERSION")
            sync = await session.get(CalendarSync, current.meeting_id)
            if sync.state == "synced":
                return {"meeting_id": current.meeting_id, "state": "synced", "version": current.version}
            sync.state, sync.applied_version, sync.last_error_code = "synced", current.version, None
            meeting = await session.get(Meeting, current.meeting_id)
            meeting.google_event_url = event.get("htmlLink")
            meeting.google_meet_url = event["hangoutLink"]
            session.add(AuditLog(actor_user_id=actor_id, action="calendar.legacy.adopted",
                entity_type="meeting", entity_id=str(current.meeting_id), created_at=utc(self.clock())))
            return {"meeting_id": current.meeting_id, "state": "synced", "version": current.version}
        return await run_sqlite_write(self.sessions, finish)
