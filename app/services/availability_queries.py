"""Read-only, bounded availability views; they do not reserve any time.

DB snapshots close before Google requests. Re-read local occupancy, identity,
settings, schedule and meeting binding before returning. Mutations recheck again.
"""
from dataclasses import dataclass
from datetime import date as Date, datetime, time, timedelta

from sqlalchemy import select, text

from app.db.models import BookingRequest, CalendarSync, Meeting, OwnerProfile, ScheduleRule, User
from app.domain.booking import MOSCOW_TZ
from app.domain.booking_policy import BookingPolicyError, check_local, scheduled_intervals
from app.domain.intervals import Interval, utc
from app.services.availability import AvailabilityPlan, CalendarUnavailable, SnapshotChanged, profile_key
from app.services.booking import ACTIVE, BookingError, check_version


@dataclass(frozen=True)
class DayAvailability:
    date: Date
    available_slots_count: int


@dataclass(frozen=True)
class AvailabilityView:
    settings_version: int
    checked_at: datetime
    from_date: Date
    to_date: Date
    horizon_end: Date
    duration_minutes: float
    days: tuple[DayAvailability, ...]
    slots: tuple[Interval, ...]
    timezone: str = "Europe/Moscow"


@dataclass(frozen=True)
class ReadState:
    profile: OwnerProfile
    rules: tuple
    occupied: tuple[Interval, ...]
    from_date: Date
    to_date: Date
    duration: float
    exclude_event_id: str | None
    meeting_key: tuple

    @property
    def fingerprint(self):
        rules = tuple(sorted((r.weekday, r.start_time, r.end_time, r.is_active) for r in self.rules))
        return profile_key(self.profile), rules, self.meeting_key, self.from_date, self.to_date


class AvailabilityQueries:
    def __init__(self, commands):
        self.sessions = commands.sessions
        self.owner_telegram_id = commands.owner_telegram_id
        self.availability = commands.availability
        self.environment = commands.calendar_environment
        self.clock = commands.clock

    async def _read(self, actor_id, first, last, expected_version, meeting_id):
        now = utc(self.clock())
        async with self.sessions() as session:
            # SQLite's legacy SELECT transaction mode otherwise does not provide
            # a consistent multi-query snapshot. No write reservation is taken.
            await session.execute(text("BEGIN"))
            actor = await session.get(User, actor_id)
            if actor is None or actor.status != "active":
                raise BookingError("ACCOUNT_BLOCKED")
            owner = actor.telegram_id == self.owner_telegram_id
            if not owner and (not actor.email or not actor.consent_at):
                raise BookingError("REGISTRATION_REQUIRED")
            profile = await session.scalar(select(OwnerProfile).join(User).where(
                User.telegram_id == self.owner_telegram_id))
            if profile is None:
                raise BookingError("BOOKING_UNAVAILABLE")
            if expected_version is not None:
                check_version(profile, expected_version)
            if not owner and not profile.booking_enabled:
                raise BookingError("BOOKING_DISABLED")
            today = now.astimezone(MOSCOW_TZ).date()
            first = first if first is not None else today
            horizon_end = today+timedelta(days=profile.booking_horizon_days)
            last = last if last is not None else max(first, min(first+timedelta(days=30), horizon_end))
            # A caller's explicit range is bounded, even if much of it lies
            # outside the booking horizon. Such days simply have no slots.
            if type(first) is not Date or type(last) is not Date or not 0 <= (last-first).days <= 30:
                raise BookingError("VALIDATION_FAILED")
            duration = profile.default_duration
            exclude_event_id, meeting_key = None, ()
            if meeting_id is not None:
                if type(meeting_id) is not int or meeting_id <= 0:
                    raise BookingError("VALIDATION_FAILED")
                meeting = await session.get(Meeting, meeting_id)
                if meeting is None or (not owner and meeting.client_id != actor_id):
                    raise BookingError("RESOURCE_NOT_FOUND")
                if meeting.status not in ACTIVE or utc(meeting.start_at) <= now:
                    raise BookingError("INVALID_STATE")
                if not owner and meeting.status in ("reschedule_pending", "rescheduled_pending"):
                    raise BookingError("INVALID_STATE")
                sync = await session.get(CalendarSync, meeting.id)
                if sync and (sync.environment != self.environment or sync.calendar_id != profile.calendar_id):
                    raise BookingError("CALENDAR_UNAVAILABLE")
                duration = (utc(meeting.end_at)-utc(meeting.start_at)).total_seconds()/60
                if duration <= 0:
                    raise BookingError("INVALID_STATE")
                exclude_event_id = sync.deterministic_event_id if sync else meeting.google_event_id
                meeting_key = (meeting.id, meeting.version, meeting.status, utc(meeting.start_at),
                               utc(meeting.end_at), exclude_event_id)
            rules = tuple((await session.scalars(select(ScheduleRule).where(
                ScheduleRule.owner_id == profile.user_id))).all())
            requests = (await session.scalars(select(BookingRequest).where(
                BookingRequest.status == "pending", BookingRequest.reserved_until > now))).all()
            meetings = (await session.scalars(select(Meeting).where(Meeting.status.in_(ACTIVE)))).all()
            occupied = tuple(Interval(row.start_at, row.end_at) for row in requests) + tuple(
                Interval(row.start_at, row.end_at) for row in meetings if row.id != meeting_id)
            return ReadState(profile, rules, occupied, first, last, duration, exclude_event_id, meeting_key)

    @staticmethod
    def _candidates(state, now):
        result = []
        for offset in range((state.to_date-state.from_date).days+1):
            day = state.from_date+timedelta(days=offset)
            for interval in scheduled_intervals(state.profile, state.rules, day, now, duration_minutes=state.duration):
                if state.meeting_key and interval.start == state.meeting_key[3]:
                    continue  # a reschedule must actually change the start time
                try:
                    check_local(state.profile, interval, state.occupied)
                except BookingPolicyError as error:
                    if error.code not in ("SLOT_UNAVAILABLE", "DAILY_LIMIT_REACHED"):
                        raise
                else:
                    result.append(interval)
        return result

    async def days(self, *, actor_id, from_date=None, to_date=None, settings_version=None, meeting_id=None):
        return await self._view(actor_id, from_date, to_date, settings_version, meeting_id)

    async def slots(self, *, actor_id, date, settings_version=None, meeting_id=None):
        if type(date) is not Date:
            raise BookingError("VALIDATION_FAILED")
        return await self._view(actor_id, date, date, settings_version, meeting_id)

    async def _view(self, actor_id, first, last, expected_version, meeting_id):
        if any(value is not None and type(value) is not Date for value in (first, last)):
            raise BookingError("VALIDATION_FAILED")
        if first is not None and last is not None and not 0 <= (last-first).days <= 30:
            raise BookingError("VALIDATION_FAILED")
        for _ in range(2):
            state = await self._read(actor_id, first, last, expected_version, meeting_id)
            now = utc(self.clock())
            try:
                candidates = self._candidates(state, now)
            except BookingPolicyError as error:
                raise BookingError(error.code) from None
            interval = Interval(datetime.combine(state.from_date, time(), tzinfo=MOSCOW_TZ),
                datetime.combine(state.to_date+timedelta(days=1), time(), tzinfo=MOSCOW_TZ))
            plan = AvailabilityPlan.capture(state.profile, interval, state.exclude_event_id)
            if plan.configured and self.environment is None:
                raise BookingError("CALENDAR_UNAVAILABLE")
            # Locally full/closed dates cannot become available via Google.
            if not candidates:
                return self._result(state, now, ())
            try:
                snapshot = await self.availability.fetch(plan)
            except CalendarUnavailable:
                raise BookingError("CALENDAR_UNAVAILABLE") from None
            current = await self._read(actor_id, first, last, expected_version, meeting_id)
            now = utc(self.clock())
            if current.fingerprint != state.fingerprint:
                continue
            try:
                snapshot.validate(current.profile, interval, now, current.exclude_event_id)
            except SnapshotChanged:
                continue
            try:
                candidates = self._candidates(current, now)
            except BookingPolicyError as error:
                raise BookingError(error.code) from None
            slots = tuple(slot for slot in candidates if not any(
                slot.conflicts(period, current.profile.buffer_minutes) for period in snapshot.busy))
            return self._result(current, snapshot.checked_at, slots)
        raise BookingError("CALENDAR_UNAVAILABLE")

    def _result(self, state, checked_at, slots):
        days = []
        for offset in range((state.to_date-state.from_date).days+1):
            day = state.from_date+timedelta(days=offset)
            count = sum(slot.start.astimezone(MOSCOW_TZ).date() == day for slot in slots)
            days.append(DayAvailability(day, count))
        return AvailabilityView(state.profile.version, checked_at, state.from_date, state.to_date,
            utc(self.clock()).astimezone(MOSCOW_TZ).date()+timedelta(days=state.profile.booking_horizon_days),
            state.duration, tuple(days), tuple(slots))
