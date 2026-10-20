"""Transactional booking foundation, not yet wired to bot or HTTP.

External availability is fetched before BEGIN IMMEDIATE, then validated again
inside it. Bot/HTTP runtime wiring remains a separate implementation step.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import (
    AuditLog, BookingRequest, Meeting, OperationResult, OutboxEvent,
    OwnerProfile, ScheduleRule, User,
)
from app.db.transactions import run_sqlite_write
from app.domain.booking import MOSCOW_TZ, WORKDAYS, WORK_START, WORK_END, validate_title
from app.domain.intervals import Interval, utc
from app.services.availability import (
    AvailabilityPlan, CalendarUnavailable, GoogleAvailability, SnapshotChanged, configured,
)
from app.services.calendar_sync import enqueue_calendar_sync

ACTIVE = ("confirmed", "rescheduled", "reschedule_pending", "rescheduled_pending")


class BookingError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def check_version(entity, expected):
    if type(expected) is not int or expected < 1:
        raise BookingError("VERSION_REQUIRED")
    if entity.version != expected:
        raise BookingError("STALE_VERSION")


def comment_text(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise BookingError("VALIDATION_FAILED")
    value = value.strip()
    if len(value) > 2000 or any(ord(c) < 32 and c not in "\n\t\r" for c in value):
        raise BookingError("VALIDATION_FAILED")
    return value or None


class BookingCommands:
    def __init__(self, sessions, *, owner_telegram_id, clock=None, availability=None,
                 calendar_environment=None):
        self.sessions = sessions
        self.owner_telegram_id = owner_telegram_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.availability = availability or GoogleAvailability(clock=self.clock)
        if calendar_environment is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", calendar_environment):
            raise ValueError("Invalid calendar environment")
        self.calendar_environment = calendar_environment

    async def _run(self, actor_id, key, kind, payload, callback, *, prepare, owner=False):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise BookingError("INVALID_REQUEST")
        canonical = json.dumps({"kind": kind, "payload": payload}, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()

        async def context(session):
            actor = await session.get(User, actor_id)
            if actor is None or actor.status != "active":
                raise BookingError("ACCOUNT_BLOCKED")
            is_owner = actor.telegram_id == self.owner_telegram_id
            if owner and not is_owner:
                raise BookingError("FORBIDDEN")
            if not is_owner and (not actor.email or not actor.consent_at):
                raise BookingError("REGISTRATION_REQUIRED")
            prior = await session.scalar(select(OperationResult).where(
                OperationResult.actor_id == actor_id, OperationResult.command_id == key))
            if prior:
                if prior.payload_hash != digest:
                    raise BookingError("IDEMPOTENCY_KEY_REUSED")
                return actor, None, json.loads(prior.response_json)
            profile = await session.scalar(select(OwnerProfile).join(User).where(
                User.telegram_id == self.owner_telegram_id))
            if profile is None:
                raise BookingError("BOOKING_UNAVAILABLE")
            return actor, profile, None

        async def execute(session):
            actor, profile, replay = await context(session)
            if replay is not None:
                return replay
            now = utc(self.clock())
            result, entity_id, version = await callback(session, actor, profile, now, snapshot)
            session.add(OperationResult(actor_id=actor_id, command_id=key, kind=kind,
                payload_hash=digest, resource_id=entity_id,
                response_json=json.dumps(result, sort_keys=True), created_at=now))
            session.add(AuditLog(actor_user_id=actor_id, action=kind,
                entity_type="booking_request" if kind.startswith("request.") else "meeting",
                entity_id=str(entity_id), created_at=now))
            event_id = uuid.uuid4().hex
            # Consumed by services.outbox; runtime wiring waits for bot migration.
            for channel in ("telegram", "smtp"):
                session.add(OutboxEvent(event_id=event_id, entity_id=entity_id,
                    entity_version=version, channel=channel, kind=kind,
                    state="pending", attempts=0, next_attempt_at=now, created_at=now))
            return result

        for _ in range(2):
            async with self.sessions() as session:
                actor, profile, replay = await context(session)
                if replay is not None:
                    return replay
                interval = await prepare(session, actor, profile, utc(self.clock()))
                plan = AvailabilityPlan.capture(profile, interval)
                if plan.configured and self.calendar_environment is None:
                    raise BookingError("CALENDAR_UNAVAILABLE")
            # The read session is closed. No SQLite write lock spans this call.
            try:
                snapshot = await self.availability.fetch(plan)
            except CalendarUnavailable:
                # Another invocation may have committed this same key while Google failed.
                async with self.sessions() as session:
                    _, _, replay = await context(session)
                    if replay is not None:
                        return replay
                raise BookingError("CALENDAR_UNAVAILABLE") from None
            try:
                return await run_sqlite_write(self.sessions, execute)
            except SnapshotChanged:
                # Release/rollback before repeating any network access.
                continue
        raise BookingError("CALENDAR_UNAVAILABLE")

    @staticmethod
    def _check_calendar(snapshot, profile, interval, now):
        if not snapshot.validate(profile, interval, now):
            raise BookingError("SLOT_UNAVAILABLE")

    @staticmethod
    async def _available(session, profile, interval, now, *, exclude_request=None, exclude_meeting=None):
        requests = (await session.scalars(select(BookingRequest).where(
            BookingRequest.status == "pending", BookingRequest.reserved_until > now))).all()
        meetings = (await session.scalars(select(Meeting).where(Meeting.status.in_(ACTIVE)))).all()
        occupied = [Interval(x.start_at, x.end_at) for x in requests if x.id != exclude_request]
        occupied += [Interval(x.start_at, x.end_at) for x in meetings if x.id != exclude_meeting]
        if any(interval.conflicts(other, profile.buffer_minutes) for other in occupied):
            raise BookingError("SLOT_UNAVAILABLE")
        day = interval.start.astimezone(MOSCOW_TZ).date()
        count = sum(other.start.astimezone(MOSCOW_TZ).date() == day for other in occupied)
        if count >= profile.daily_limit:
            raise BookingError("DAILY_LIMIT_REACHED")

    @staticmethod
    async def _schedule(session, profile, interval, now):
        local = interval.start.astimezone(MOSCOW_TZ)
        end = interval.end.astimezone(MOSCOW_TZ)
        if interval.start < now + timedelta(hours=profile.min_booking_notice_hours):
            raise BookingError("NOTICE_REQUIRED")
        if not now.astimezone(MOSCOW_TZ).date() <= local.date() <= (
            now.astimezone(MOSCOW_TZ).date() + timedelta(days=profile.booking_horizon_days)
        ):
            raise BookingError("OUTSIDE_HORIZON")
        rules = (await session.scalars(select(ScheduleRule).where(
            ScheduleRule.owner_id == profile.user_id))).all()
        if rules:
            spans = sorted((r.start_time, r.end_time) for r in rules
                           if r.is_active and r.weekday == local.weekday())
        else:
            spans = [(WORK_START, WORK_END)] if local.weekday() in WORKDAYS else []
        # Merge overlapping schedule windows before checking the hourly grid.
        merged = []
        for start, finish in spans:
            if finish <= start:
                raise BookingError("INVALID_SCHEDULE")
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(finish, merged[-1][1]))
            else:
                merged.append((start, finish))
        for start, finish in merged:
            window_start = datetime.combine(local.date(), start, tzinfo=MOSCOW_TZ)
            window_end = datetime.combine(local.date(), finish, tzinfo=MOSCOW_TZ)
            if window_start <= local and end <= window_end and (local-window_start).total_seconds() % 3600 == 0:
                return
        raise BookingError("OUTSIDE_SCHEDULE")

    async def create_request(self, *, actor_id, command_id, start_at, title, comment=None, settings_version):
        if not isinstance(start_at, datetime) or start_at.tzinfo is None:
            raise BookingError("VALIDATION_FAILED")
        if not isinstance(title, str) or validate_title(title) is None:
            raise BookingError("VALIDATION_FAILED")
        title = validate_title(title)
        comment = comment_text(comment)
        start_at = utc(start_at)
        payload = dict(start_at=start_at.isoformat(), title=title, comment=comment,
                       settings_version=settings_version)

        async def prepare(session, actor, profile, now):
            if actor.telegram_id == self.owner_telegram_id:
                raise BookingError("FORBIDDEN")
            check_version(profile, settings_version)
            if not profile.booking_enabled:
                raise BookingError("BOOKING_DISABLED")
            interval = Interval(start_at, start_at + timedelta(minutes=profile.default_duration))
            await self._schedule(session, profile, interval, now)
            return interval

        async def create(session, actor, profile, now, snapshot):
            interval = await prepare(session, actor, profile, now)
            self._check_calendar(snapshot, profile, interval, now)
            await self._available(session, profile, interval, now)
            request = BookingRequest(client_id=actor.id, title=title, comment=comment,
                start_at=interval.start, end_at=interval.end, status="pending",
                reserved_until=now+timedelta(minutes=30), version=1, created_at=now)
            session.add(request)
            await session.flush()
            result = dict(id=request.id, version=1, status="pending",
                reserved_until=request.reserved_until.isoformat())
            return result, request.id, 1

        return await self._run(actor_id, command_id, "request.created", payload, create, prepare=prepare)

    async def approve_request(self, *, actor_id, command_id, request_id, expected_version):
        payload = dict(request_id=request_id, expected_version=expected_version)

        async def checked_request(session, actor, profile, now):
            request = await session.get(BookingRequest, request_id)
            if request is None:
                raise BookingError("NOT_FOUND")
            check_version(request, expected_version)
            if request.status != "pending":
                raise BookingError("INVALID_STATE")
            client = await session.get(User, request.client_id)
            if not client or client.status != "active" or not client.email or not client.consent_at:
                raise BookingError("CLIENT_UNAVAILABLE")
            interval = Interval(request.start_at, request.end_at)
            if interval.start <= now:
                raise BookingError("INVALID_STATE")
            return request, interval

        async def prepare(session, actor, profile, now):
            _, interval = await checked_request(session, actor, profile, now)
            return interval

        async def approve(session, actor, profile, now, snapshot):
            request, interval = await checked_request(session, actor, profile, now)
            self._check_calendar(snapshot, profile, interval, now)
            await self._available(session, profile, interval, now, exclude_request=request.id)
            request.status = "approved"
            request.approved_at = now
            request.version += 1
            meeting = Meeting(booking_request_id=request.id, client_id=request.client_id,
                title=request.title, start_at=request.start_at, end_at=request.end_at,
                timezone=request.timezone, status="confirmed", version=1, created_at=now)
            session.add(meeting)
            await session.flush()
            if configured(profile):
                await enqueue_calendar_sync(session, meeting=meeting, profile=profile,
                    environment=self.calendar_environment, now=now)
            return dict(id=request.id, version=request.version, status="approved",
                        meeting_id=meeting.id), request.id, request.version

        return await self._run(actor_id, command_id, "request.approved", payload, approve,
                               prepare=prepare, owner=True)
