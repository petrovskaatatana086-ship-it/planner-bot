"""Durable Google desired state, consumed by the fenced outbox worker.

Registered by the opt-in runtime. Call enqueue_calendar_sync only inside
an existing booking write transaction. No network call belongs in that transaction.
"""
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import select, update

from app.db.models import BookingRequest, CalendarSync, Meeting, OutboxEvent, OwnerProfile, User
from app.domain.intervals import utc
from app.integrations.google_events import GoogleFailure
from app.services.outbox import DeliveryError, DeliveryResult


async def enqueue_ready_notices(session, meeting, now):
    request = await session.get(BookingRequest, meeting.booking_request_id)
    if request is None or request.status != "approved":
        return
    originals = (await session.scalars(select(OutboxEvent).where(
        OutboxEvent.entity_id == request.id, OutboxEvent.entity_version == request.version,
        OutboxEvent.kind == "request.approved", OutboxEvent.state == "sent",
        OutboxEvent.error_code == "CALENDAR_DELAY_NOTICE"))).all()
    for original in originals:
        exists = await session.scalar(select(OutboxEvent.id).where(
            OutboxEvent.entity_id == request.id, OutboxEvent.entity_version == request.version,
            OutboxEvent.kind == "request.calendar_ready", OutboxEvent.channel == original.channel))
        if exists is None:
            session.add(OutboxEvent(event_id=uuid.uuid4().hex, entity_id=request.id,
                entity_version=request.version, channel=original.channel, kind="request.calendar_ready",
                state="pending", attempts=0, next_attempt_at=now, created_at=now))
    move_notices = (await session.scalars(select(OutboxEvent).where(
        OutboxEvent.entity_id == meeting.id, OutboxEvent.entity_version == meeting.version,
        OutboxEvent.kind == "meeting.rescheduled", OutboxEvent.state == "sent",
        OutboxEvent.error_code == "CALENDAR_DELAY_NOTICE"))).all()
    for original in move_notices:
        prior = await session.scalar(select(OutboxEvent.id).where(
            OutboxEvent.entity_id == meeting.id, OutboxEvent.entity_version == meeting.version,
            OutboxEvent.kind == "meeting.calendar_ready", OutboxEvent.channel == original.channel))
        if prior is None:
            session.add(OutboxEvent(event_id=uuid.uuid4().hex, entity_id=meeting.id,
                entity_version=meeting.version, channel=original.channel, kind="meeting.calendar_ready",
                state="pending", attempts=0, next_attempt_at=now, created_at=now))


async def enqueue_calendar_sync(session, *, meeting, profile, environment, now, allow_unavailable=False):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", environment):
        raise ValueError("Invalid calendar environment")
    sync = await session.get(CalendarSync, meeting.id)
    if (not profile.calendar_id or profile.google_status != "active") and not (allow_unavailable and sync):
        raise DeliveryError("CALENDAR_UNAVAILABLE")
    if sync is None:
        sync = CalendarSync(meeting_id=meeting.id, environment=environment,
            calendar_id=profile.calendar_id,
            deterministic_event_id=meeting.google_event_id or uuid.uuid4().hex,
            conference_request_id=uuid.uuid4().hex, desired_version=meeting.version,
            applied_version=0, state="pending")
        session.add(sync)
    elif sync.environment != environment or (sync.calendar_id != profile.calendar_id and not allow_unavailable):
        raise DeliveryError("CALENDAR_BINDING_CHANGED", permanent=True)
    elif sync.desired_version > meeting.version:
        raise DeliveryError("CALENDAR_VERSION_MISMATCH", permanent=True)
    elif sync.desired_version < meeting.version:
        sync.desired_version = meeting.version
        sync.state = "pending"
        sync.last_error_code = None
    await session.flush()
    if meeting.status.startswith("cancelled"):
        # Cancellation supersedes unsuccessful creates/updates, but never steals an active lease.
        await session.execute(update(OutboxEvent).where(
            OutboxEvent.channel == "google", OutboxEvent.kind == "meeting.sync",
            OutboxEvent.entity_id == meeting.id, OutboxEvent.entity_version < meeting.version,
            OutboxEvent.state.in_(("pending", "retry", "failed"))
        ).values(state="superseded", lease_until=None, lease_token=None))
    prior = await session.scalar(select(OutboxEvent.id).where(
        OutboxEvent.channel == "google", OutboxEvent.kind == "meeting.sync",
        OutboxEvent.entity_id == meeting.id, OutboxEvent.entity_version == meeting.version))
    if prior is None:
        session.add(OutboxEvent(event_id=uuid.uuid4().hex, entity_id=meeting.id,
            entity_version=meeting.version, channel="google", kind="meeting.sync",
            state="pending", attempts=0, next_attempt_at=now, created_at=now))
    await session.flush()
    return sync


@dataclass(frozen=True)
class Snapshot:
    meeting_id: int
    version: int
    calendar_id: str
    event_id: str
    conference_id: str
    title: str
    email: str
    start: datetime
    end: datetime
    cancelled: bool
    applied_version: int
    applied_start: datetime | None
    applied_end: datetime | None


def remote_time(event, field):
    try:
        value = datetime.fromisoformat(event[field]["dateTime"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("offset required")
        return utc(value)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise DeliveryError("CALENDAR_INVALID_RESPONSE") from None


def safe_link(value, hosts):
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password:
        raise DeliveryError("CALENDAR_INVALID_RESPONSE")
    return value


class CalendarDeliveries:
    def __init__(self, sessions, *, gateway, owner_telegram_id, environment, clock=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", environment):
            raise ValueError("Invalid calendar environment")
        self.sessions = sessions
        self.gateway = gateway
        self.owner_telegram_id = owner_telegram_id
        self.environment = environment
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def handlers(self):
        return {("google", "meeting.sync"): self.deliver}

    async def _snapshot(self, delivery):
        async with self.sessions() as session:
            sync = await session.get(CalendarSync, delivery.entity_id)
            meeting = await session.get(Meeting, delivery.entity_id)
            if sync is None or meeting is None:
                raise DeliveryError("CALENDAR_STATE_MISSING", permanent=True)
            if sync.environment != self.environment:
                raise DeliveryError("CALENDAR_ENVIRONMENT_MISMATCH", permanent=True)
            profile = await session.scalar(select(OwnerProfile).join(User).where(
                User.telegram_id == self.owner_telegram_id))
            if profile is None or profile.calendar_id != sync.calendar_id or profile.google_status != "active":
                raise DeliveryError("CALENDAR_BINDING_CHANGED", permanent=True)
            if sync.desired_version != meeting.version or delivery.entity_version > meeting.version:
                raise DeliveryError("CALENDAR_VERSION_MISMATCH", permanent=True)
            client = await session.get(User, meeting.client_id)
            cancelled = meeting.status.startswith("cancelled")
            if (client is None or not client.email) and not cancelled:
                raise DeliveryError("CALENDAR_CLIENT_MISSING", permanent=True)
            if meeting.status not in ("confirmed", "rescheduled", "reschedule_pending",
                                      "cancelled_by_client", "cancelled_by_owner"):
                raise DeliveryError("CALENDAR_INVALID_STATE", permanent=True)
            return Snapshot(meeting.id, meeting.version, sync.calendar_id,
                sync.deterministic_event_id, sync.conference_request_id, meeting.title,
                client.email if client else "", utc(meeting.start_at), utc(meeting.end_at),
                cancelled, sync.applied_version,
                utc(sync.applied_start_at) if sync.applied_start_at else None,
                utc(sync.applied_end_at) if sync.applied_end_at else None)

    def _tags(self, snapshot):
        return {"meetly_environment": self.environment, "meetly_meeting_id": str(snapshot.meeting_id),
                "meetly_version": str(snapshot.version)}

    def _owned(self, snapshot, event):
        properties = event.get("extendedProperties")
        tags = properties.get("private") if isinstance(properties, dict) else None
        if not isinstance(tags, dict):
            raise DeliveryError("CALENDAR_OWNERSHIP_CONFLICT", permanent=True)
        if (event.get("id") != snapshot.event_id or tags.get("meetly_environment") != self.environment
                or tags.get("meetly_meeting_id") != str(snapshot.meeting_id)):
            # Legacy events without a marker require explicit migration/adoption, never blind overwrite.
            raise DeliveryError("CALENDAR_OWNERSHIP_CONFLICT", permanent=True)

    def _body(self, snapshot):
        return {"summary": snapshot.title,
            "start": {"dateTime": snapshot.start.isoformat(), "timeZone": "Europe/Moscow"},
            "end": {"dateTime": snapshot.end.isoformat(), "timeZone": "Europe/Moscow"},
            "attendees": [{"email": snapshot.email}],
            "extendedProperties": {"private": self._tags(snapshot)}}

    def _success(self, snapshot, event=None, *, ready=True):
        calendar_url = safe_link(event.get("htmlLink"), {"calendar.google.com", "www.google.com"}) if event else None
        meet_url = safe_link(event.get("hangoutLink"), {"meet.google.com"}) if event else None

        async def apply(session):
            sync = await session.get(CalendarSync, snapshot.meeting_id)
            meeting = await session.get(Meeting, snapshot.meeting_id)
            if sync is None or meeting is None:
                return
            if sync.environment != self.environment or sync.calendar_id != snapshot.calendar_id:
                return
            # Store observed progress without claiming that a newer desired version was applied.
            if sync.applied_version <= snapshot.version:
                sync.applied_version = snapshot.version
                sync.applied_start_at = snapshot.start
                sync.applied_end_at = snapshot.end
            profile = await session.scalar(select(OwnerProfile).join(User).where(
                User.telegram_id == self.owner_telegram_id))
            current_binding = profile and profile.calendar_id == snapshot.calendar_id and profile.google_status == "active"
            if not current_binding:
                sync.state = "failed"
                sync.last_error_code = "CALENDAR_BINDING_CHANGED"
            elif ready and sync.desired_version == snapshot.version and meeting.version == snapshot.version:
                sync.state = "synced"
                sync.last_error_code = None
                meeting.google_event_id = snapshot.event_id
                meeting.google_event_url = calendar_url
                meeting.google_meet_url = meet_url
                if meet_url:
                    # Covers calendar finishing after a delay notice was acknowledged.
                    await enqueue_ready_notices(session, meeting, utc(self.clock()))
            else:
                sync.state = "pending"
        return DeliveryResult(on_commit=apply)

    def _failure_hook(self, snapshot, delivery, error):
        async def apply(session):
            sync = await session.get(CalendarSync, snapshot.meeting_id)
            if (sync is None or sync.environment != self.environment or sync.calendar_id != snapshot.calendar_id
                    or sync.desired_version != snapshot.version):
                return
            sync.last_error_code = error.code
            sync.state = "failed" if error.permanent or delivery.attempts >= 6 else "pending"
        return apply

    async def _sync(self, snapshot):
        event = await self.gateway.get(snapshot.calendar_id, snapshot.event_id)
        if snapshot.cancelled:
            if event is not None and event.get("status") != "cancelled":
                self._owned(snapshot, event)
                if not event.get("etag"):
                    raise DeliveryError("CALENDAR_INVALID_RESPONSE")
                await self.gateway.delete(snapshot.calendar_id, snapshot.event_id, event["etag"])
            return self._success(snapshot)
        if event is None:
            if snapshot.applied_version:
                raise DeliveryError("CALENDAR_EVENT_REMOVED", permanent=True)
            body = self._body(snapshot)
            body.update(id=snapshot.event_id, conferenceData={"createRequest": {
                "requestId": snapshot.conference_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"}}})
            try:
                event = await self.gateway.insert(snapshot.calendar_id, body)
            except GoogleFailure as error:
                if error.status != 409:
                    raise
                event = await self.gateway.get(snapshot.calendar_id, snapshot.event_id)
                if event is None:
                    raise DeliveryError("CALENDAR_CONFLICT_UNRESOLVED")
        if event.get("status") == "cancelled":
            raise DeliveryError("CALENDAR_EVENT_REMOVED", permanent=True)
        self._owned(snapshot, event)
        try:
            remote_version = int(event["extendedProperties"]["private"]["meetly_version"])
        except (KeyError, TypeError, ValueError):
            raise DeliveryError("CALENDAR_INVALID_RESPONSE") from None
        matches = remote_time(event, "start") == snapshot.start and remote_time(event, "end") == snapshot.end
        if remote_version == snapshot.version:
            if not matches:
                raise DeliveryError("CALENDAR_EXTERNAL_EDIT", permanent=True)
        elif remote_version == snapshot.applied_version and snapshot.applied_version > 0:
            if (remote_time(event, "start") != snapshot.applied_start
                    or remote_time(event, "end") != snapshot.applied_end):
                raise DeliveryError("CALENDAR_EXTERNAL_EDIT", permanent=True)
            if not event.get("etag"):
                raise DeliveryError("CALENDAR_INVALID_RESPONSE")
            body = self._body(snapshot)
            # Preserve private properties belonging to other tools.
            body["extendedProperties"]["private"] = {**event.get("extendedProperties", {}).get("private", {}),
                                                       **self._tags(snapshot)}
            dates_changed = (snapshot.start != snapshot.applied_start or snapshot.end != snapshot.applied_end)
            event = await self.gateway.patch(snapshot.calendar_id, snapshot.event_id, body, event["etag"],
                                             notify=dates_changed)
            self._owned(snapshot, event)
            if (remote_time(event, "start") != snapshot.start or remote_time(event, "end") != snapshot.end
                    or event["extendedProperties"]["private"].get("meetly_version") != str(snapshot.version)):
                raise DeliveryError("CALENDAR_INVALID_RESPONSE")
        else:
            raise DeliveryError("CALENDAR_VERSION_CONFLICT", permanent=True)
        conference = event.get("conferenceData", {}).get("createRequest", {}).get("status", {}).get("statusCode")
        if conference == "failure":
            raise DeliveryError("CONFERENCE_FAILED", permanent=True)
        if not event.get("hangoutLink"):
            raise DeliveryError("CONFERENCE_PENDING",
                                on_commit=self._success(snapshot, event, ready=False).on_commit)
        return self._success(snapshot, event)

    async def deliver(self, delivery):
        try:
            snapshot = await self._snapshot(delivery)
        except DeliveryError as error:
            async def record_failure(session, code=error.code):
                sync = await session.get(CalendarSync, delivery.entity_id)
                if (sync and sync.environment == self.environment
                        and sync.desired_version == delivery.entity_version):
                    sync.state = "failed"
                    sync.last_error_code = code
            error.on_commit = record_failure
            raise
        try:
            return await self._sync(snapshot)
        except GoogleFailure as failure:
            permanent = failure.status in (400, 401, 403, 404, 410, 412) and not failure.quota
            code = "CALENDAR_EXTERNAL_EDIT" if failure.status == 412 else "CALENDAR_UNAVAILABLE"
            error = DeliveryError(code, permanent=permanent, retry_after=failure.retry_after)
        except DeliveryError as failure:
            error = failure
        progress = error.on_commit
        failure_hook = self._failure_hook(snapshot, delivery, error)
        async def apply(session):
            if progress is not None:
                await progress(session)
            await failure_hook(session)
        error.on_commit = apply
        raise error
