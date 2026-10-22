"""Transactional booking commands shared by bot and future HTTP adapter.

External availability is fetched before BEGIN IMMEDIATE, then validated again
inside it. Bot activation is explicit; HTTP wiring remains separate.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import (
    AuditLog, BookingRequest, Meeting, OperationResult, OutboxEvent,
    OwnerProfile, ScheduleRule, User, CalendarSync, RescheduleRequest,
)
from app.db.transactions import run_sqlite_write
from app.domain.booking import MOSCOW_TZ, WORKDAYS, WORK_START, WORK_END, validate_title
from app.domain.intervals import Interval, utc
from app.domain.booking_policy import BookingPolicyError, check_local, check_schedule
from app.services.availability import (
    AvailabilityPlan, AvailabilityTarget, CalendarUnavailable, GoogleAvailability, SnapshotChanged, configured,
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

    async def _run(self, actor_id, key, kind, payload, callback, *, prepare=None, owner=False):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise BookingError("INVALID_REQUEST")
        canonical = json.dumps({"kind": kind, "payload": payload}, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        snapshot = None

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

        if prepare is None:
            return await run_sqlite_write(self.sessions, execute)
        for _ in range(2):
            async with self.sessions() as session:
                actor, profile, replay = await context(session)
                if replay is not None:
                    return replay
                target = await prepare(session, actor, profile, utc(self.clock()))
                if isinstance(target, Interval):
                    target = AvailabilityTarget(target)
                plan = AvailabilityPlan.capture(profile, target.interval, target.exclude_event_id)
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
    def _check_calendar(snapshot, profile, interval, now, exclude_event_id=None):
        if not snapshot.validate(profile, interval, now, exclude_event_id):
            raise BookingError("SLOT_UNAVAILABLE")

    @staticmethod
    async def _available(session, profile, interval, now, *, exclude_request=None, exclude_meeting=None):
        requests = (await session.scalars(select(BookingRequest).where(
            BookingRequest.status == "pending", BookingRequest.reserved_until > now))).all()
        meetings = (await session.scalars(select(Meeting).where(Meeting.status.in_(ACTIVE)))).all()
        occupied = [Interval(x.start_at, x.end_at) for x in requests if x.id != exclude_request]
        occupied += [Interval(x.start_at, x.end_at) for x in meetings if x.id != exclude_meeting]
        try:
            check_local(profile, interval, occupied)
        except BookingPolicyError as error:
            raise BookingError(error.code) from None

    @staticmethod
    async def _schedule(session, profile, interval, now):
        rules = (await session.scalars(select(ScheduleRule).where(
            ScheduleRule.owner_id == profile.user_id))).all()
        try:
            check_schedule(profile, rules, interval, now)
        except BookingPolicyError as error:
            raise BookingError(error.code) from None

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

    async def _meeting(self, session, actor, meeting_id, expected_version, now):
        meeting = await session.get(Meeting, meeting_id)
        if meeting is None or (actor.telegram_id != self.owner_telegram_id and meeting.client_id != actor.id):
            raise BookingError("NOT_FOUND")
        check_version(meeting, expected_version)
        if meeting.status not in ("confirmed", "rescheduled", "reschedule_pending") or utc(meeting.start_at) <= now:
            raise BookingError("INVALID_STATE")
        return meeting

    async def _sync_change(self, session, meeting, profile, now, *, allow_unavailable=False):
        sync = await session.get(CalendarSync, meeting.id)
        if allow_unavailable and sync is None and not meeting.google_event_id and profile.google_status != "active":
            return  # no external event was ever scheduled for this local-only meeting
        if configured(profile) or sync is not None or meeting.google_event_id:
            if self.calendar_environment is None:
                raise BookingError("CALENDAR_UNAVAILABLE")
            await enqueue_calendar_sync(session, meeting=meeting, profile=profile,
                environment=self.calendar_environment, now=now, allow_unavailable=allow_unavailable)

    async def _move_target(self, session, meeting, profile, start_at, now):
        interval = Interval(utc(start_at), utc(start_at)+(utc(meeting.end_at)-utc(meeting.start_at)))
        if interval.start == utc(meeting.start_at):
            raise BookingError("VALIDATION_FAILED")
        await self._schedule(session, profile, interval, now)
        event_id = meeting.google_event_id
        sync = await session.get(CalendarSync, meeting.id)
        if sync:
            if sync.calendar_id != profile.calendar_id or sync.environment != self.calendar_environment:
                raise BookingError("CALENDAR_UNAVAILABLE")
            event_id = sync.deterministic_event_id
        return AvailabilityTarget(interval, event_id)

    @staticmethod
    async def _pending_reschedule(session, meeting, identifier=None):
        proposal = await session.scalar(select(RescheduleRequest).where(
            RescheduleRequest.meeting_id == meeting.id, RescheduleRequest.status == "pending"))
        if proposal is None or (identifier is not None and proposal.id != identifier):
            raise BookingError("INVALID_STATE")
        return proposal

    @staticmethod
    def _close_proposal(meeting, proposal, status, now, reason=None):
        proposal.status = status
        proposal.reason = reason
        proposal.decided_at = now
        meeting.proposed_start_at = None
        meeting.proposed_end_at = None
        meeting.reschedule_comment = None

    @staticmethod
    def _aware_start(value):
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BookingError("VALIDATION_FAILED")
        return utc(value)

    async def reject_request(self, *, actor_id, command_id, request_id, expected_version, reason):
        reason = comment_text(reason)
        if not reason:
            raise BookingError("VALIDATION_FAILED")
        payload = dict(request_id=request_id, expected_version=expected_version, reason=reason)
        async def reject(session, actor, profile, now, snapshot):
            request = await session.get(BookingRequest, request_id)
            if request is None:
                raise BookingError("NOT_FOUND")
            check_version(request, expected_version)
            if request.status != "pending":
                raise BookingError("INVALID_STATE")
            request.status = "rejected"
            request.rejection_reason = reason
            request.rejected_at = now
            request.version += 1
            return dict(id=request.id, version=request.version, status=request.status), request.id, request.version
        return await self._run(actor_id, command_id, "request.rejected", payload, reject, owner=True)

    async def request_reschedule(self, *, actor_id, command_id, meeting_id, expected_version,
                                 start_at, settings_version, comment=None):
        start_at = self._aware_start(start_at)
        comment = comment_text(comment)
        payload = dict(meeting_id=meeting_id, expected_version=expected_version,
                       start_at=start_at.isoformat(), settings_version=settings_version, comment=comment)
        async def prepare(session, actor, profile, now):
            if actor.telegram_id == self.owner_telegram_id:
                raise BookingError("FORBIDDEN")
            meeting = await self._meeting(session, actor, meeting_id, expected_version, now)
            check_version(profile, settings_version)
            if not profile.booking_enabled:
                raise BookingError("BOOKING_DISABLED")
            existing = await session.scalar(select(RescheduleRequest.id).where(
                RescheduleRequest.meeting_id == meeting.id, RescheduleRequest.status == "pending"))
            if meeting.status == "reschedule_pending" or existing:
                raise BookingError("INVALID_STATE")
            return await self._move_target(session, meeting, profile, start_at, now)
        async def propose(session, actor, profile, now, snapshot):
            target = await prepare(session, actor, profile, now)
            self._check_calendar(snapshot, profile, target.interval, now, target.exclude_event_id)
            await self._available(session, profile, target.interval, now, exclude_meeting=meeting_id)
            meeting = await session.get(Meeting, meeting_id)
            proposal = RescheduleRequest(meeting_id=meeting.id, requested_by_id=actor.id,
                proposed_start_at=target.interval.start, proposed_end_at=target.interval.end,
                previous_status=meeting.status, comment=comment, status="pending", created_at=now)
            session.add(proposal)
            meeting.status = "reschedule_pending"
            meeting.proposed_start_at = target.interval.start
            meeting.proposed_end_at = target.interval.end
            meeting.reschedule_comment = comment
            meeting.version += 1
            await self._sync_change(session, meeting, profile, now)
            await session.flush()
            return dict(id=meeting.id, version=meeting.version, status=meeting.status,
                        reschedule_request_id=proposal.id), meeting.id, meeting.version
        return await self._run(actor_id, command_id, "meeting.reschedule_requested", payload, propose, prepare=prepare)

    async def approve_reschedule(self, *, actor_id, command_id, meeting_id, expected_version,
                                 reschedule_request_id):
        payload = dict(meeting_id=meeting_id, expected_version=expected_version,
                       reschedule_request_id=reschedule_request_id)
        async def prepare(session, actor, profile, now):
            meeting = await self._meeting(session, actor, meeting_id, expected_version, now)
            if meeting.status != "reschedule_pending":
                raise BookingError("INVALID_STATE")
            client = await session.get(User, meeting.client_id)
            if client is None or client.status != "active" or not client.email or not client.consent_at:
                raise BookingError("CLIENT_UNAVAILABLE")
            proposal = await self._pending_reschedule(session, meeting, reschedule_request_id)
            target = await self._move_target(session, meeting, profile, proposal.proposed_start_at, now)
            if target.interval.end != utc(proposal.proposed_end_at):
                raise BookingError("INVALID_STATE")
            return target
        async def approve(session, actor, profile, now, snapshot):
            target = await prepare(session, actor, profile, now)
            self._check_calendar(snapshot, profile, target.interval, now, target.exclude_event_id)
            await self._available(session, profile, target.interval, now, exclude_meeting=meeting_id)
            meeting = await session.get(Meeting, meeting_id)
            proposal = await self._pending_reschedule(session, meeting, reschedule_request_id)
            self._close_proposal(meeting, proposal, "approved", now)
            meeting.start_at, meeting.end_at = target.interval.start, target.interval.end
            meeting.status = "rescheduled"
            meeting.version += 1
            await self._sync_change(session, meeting, profile, now)
            return dict(id=meeting.id, version=meeting.version, status=meeting.status,
                        reschedule_request_id=proposal.id), meeting.id, meeting.version
        return await self._run(actor_id, command_id, "meeting.rescheduled", payload, approve, prepare=prepare, owner=True)

    async def reject_reschedule(self, *, actor_id, command_id, meeting_id, expected_version,
                                reschedule_request_id, reason):
        reason = comment_text(reason)
        if not reason:
            raise BookingError("VALIDATION_FAILED")
        payload = dict(meeting_id=meeting_id, expected_version=expected_version,
                       reschedule_request_id=reschedule_request_id, reason=reason)
        async def reject(session, actor, profile, now, snapshot):
            meeting = await self._meeting(session, actor, meeting_id, expected_version, now)
            if meeting.status != "reschedule_pending":
                raise BookingError("INVALID_STATE")
            proposal = await self._pending_reschedule(session, meeting, reschedule_request_id)
            if proposal.previous_status not in ("confirmed", "rescheduled"):
                raise BookingError("LEGACY_RESCHEDULE_REVIEW_REQUIRED")
            self._close_proposal(meeting, proposal, "rejected", now, reason)
            meeting.status = proposal.previous_status
            meeting.version += 1
            await self._sync_change(session, meeting, profile, now, allow_unavailable=True)
            return dict(id=meeting.id, version=meeting.version, status=meeting.status,
                        reschedule_request_id=proposal.id), meeting.id, meeting.version
        return await self._run(actor_id, command_id, "meeting.reschedule_rejected", payload, reject, owner=True)

    async def reschedule_by_owner(self, *, actor_id, command_id, meeting_id, expected_version,
                                  start_at, settings_version, reason=None):
        start_at = self._aware_start(start_at)
        reason = comment_text(reason)
        payload = dict(meeting_id=meeting_id, expected_version=expected_version, start_at=start_at.isoformat(),
                       settings_version=settings_version, reason=reason)
        async def prepare(session, actor, profile, now):
            meeting = await self._meeting(session, actor, meeting_id, expected_version, now)
            check_version(profile, settings_version)
            return await self._move_target(session, meeting, profile, start_at, now)
        async def move(session, actor, profile, now, snapshot):
            target = await prepare(session, actor, profile, now)
            self._check_calendar(snapshot, profile, target.interval, now, target.exclude_event_id)
            await self._available(session, profile, target.interval, now, exclude_meeting=meeting_id)
            meeting = await session.get(Meeting, meeting_id)
            previous = meeting.status
            if meeting.status == "reschedule_pending":
                proposal = await self._pending_reschedule(session, meeting)
                previous = proposal.previous_status
                self._close_proposal(meeting, proposal, "superseded", now, reason)
                await session.flush()  # release partial unique pending index before a new history row
            session.add(RescheduleRequest(meeting_id=meeting.id, requested_by_id=actor.id,
                proposed_start_at=target.interval.start, proposed_end_at=target.interval.end,
                previous_status=previous, status="approved", comment=reason, created_at=now, decided_at=now))
            meeting.start_at, meeting.end_at = target.interval.start, target.interval.end
            meeting.status = "rescheduled"
            meeting.version += 1
            await self._sync_change(session, meeting, profile, now)
            return dict(id=meeting.id, version=meeting.version, status=meeting.status), meeting.id, meeting.version
        return await self._run(actor_id, command_id, "meeting.rescheduled", payload, move, prepare=prepare, owner=True)

    async def cancel_meeting(self, *, actor_id, command_id, meeting_id, expected_version, reason=None):
        reason = comment_text(reason)
        payload = dict(meeting_id=meeting_id, expected_version=expected_version, reason=reason)
        async def cancel(session, actor, profile, now, snapshot):
            meeting = await self._meeting(session, actor, meeting_id, expected_version, now)
            owner = actor.telegram_id == self.owner_telegram_id
            if not owner and utc(meeting.start_at) < now+timedelta(hours=profile.min_booking_notice_hours):
                raise BookingError("NOTICE_REQUIRED")
            if meeting.status == "reschedule_pending":
                proposal = await self._pending_reschedule(session, meeting)
                self._close_proposal(meeting, proposal, "cancelled", now, reason)
            meeting.status = "cancelled_by_owner" if owner else "cancelled_by_client"
            meeting.cancellation_reason = reason
            meeting.version += 1
            await self._sync_change(session, meeting, profile, now, allow_unavailable=True)
            if not owner:
                session.add(OutboxEvent(event_id=uuid.uuid4().hex, entity_id=meeting.id,
                    entity_version=meeting.version, channel="telegram", kind="meeting.cancelled_owner",
                    state="pending", attempts=0, next_attempt_at=now, created_at=now))
            return dict(id=meeting.id, version=meeting.version, status=meeting.status), meeting.id, meeting.version
        return await self._run(actor_id, command_id, "meeting.cancelled", payload, cancel)
