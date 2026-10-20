"""Immutable, short-lived external availability snapshots; no DB or client roles."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.domain.intervals import Interval, utc
from app.integrations.google_events import GoogleFailure


class SnapshotChanged(Exception):
    """Rollback, release writer lock and fetch again outside the transaction."""


class CalendarUnavailable(Exception):
    pass


def profile_key(profile):
    # Compare actual settings too: legacy bot writers do not yet increment version.
    return (profile.user_id, profile.version, profile.google_status, profile.calendar_id,
            profile.google_email, profile.default_duration, profile.buffer_minutes,
            profile.daily_limit, profile.booking_horizon_days,
            profile.min_booking_notice_hours, profile.booking_enabled)


def configured(profile):
    return bool(profile.calendar_id or profile.google_status == "active")


@dataclass(frozen=True)
class AvailabilityPlan:
    fingerprint: tuple
    calendar_id: str | None
    configured: bool
    active: bool
    interval: Interval
    buffer_minutes: int

    @classmethod
    def capture(cls, profile, interval):
        return cls(profile_key(profile), profile.calendar_id, configured(profile),
                   profile.google_status == "active", interval, profile.buffer_minutes)


@dataclass(frozen=True)
class AvailabilitySnapshot:
    plan: AvailabilityPlan
    checked_at: datetime
    busy: tuple[Interval, ...]

    def validate(self, profile, interval, now):
        if profile_key(profile) != self.plan.fingerprint or interval != self.plan.interval:
            raise SnapshotChanged()
        if self.plan.configured:
            age = (utc(now)-utc(self.checked_at)).total_seconds()
            if not 0 <= age <= 5:
                raise SnapshotChanged()
        return not any(interval.conflicts(period, profile.buffer_minutes) for period in self.busy)


class GoogleAvailability:
    def __init__(self, gateway=None, *, clock=None):
        self.gateway = gateway
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def fetch(self, plan):
        # Age includes network latency and writer lock waiting, not just time after response.
        checked_at = utc(self.clock())
        if not plan.configured:
            return AvailabilitySnapshot(plan, checked_at, ())
        if self.gateway is None or not plan.active or not plan.calendar_id:
            raise CalendarUnavailable()
        gap = timedelta(minutes=plan.buffer_minutes)
        try:
            periods = await self.gateway.busy(plan.calendar_id, plan.interval.start-gap, plan.interval.end+gap)
            if not isinstance(periods, (tuple, list)) or any(not isinstance(p, Interval) for p in periods):
                raise CalendarUnavailable()
        except GoogleFailure:
            raise CalendarUnavailable() from None
        return AvailabilitySnapshot(plan, checked_at, tuple(periods))
