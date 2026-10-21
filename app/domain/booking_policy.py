"""Schedule and local occupancy rules used by both previews and mutations."""
from datetime import datetime, timedelta

from app.domain.booking import MOSCOW_TZ, WORKDAYS, WORK_START, WORK_END
from app.domain.intervals import Interval


class BookingPolicyError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def schedule_windows(rules, day):
    spans = sorted((r.start_time, r.end_time) for r in rules
                   if r.is_active and r.weekday == day.weekday()) if rules else (
                       [(WORK_START, WORK_END)] if day.weekday() in WORKDAYS else [])
    merged = []
    for start, finish in spans:
        if finish <= start:
            raise BookingPolicyError("INVALID_SCHEDULE")
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(finish, merged[-1][1]))
        else:
            merged.append((start, finish))
    return tuple((datetime.combine(day, start, tzinfo=MOSCOW_TZ),
                  datetime.combine(day, finish, tzinfo=MOSCOW_TZ)) for start, finish in merged)


def check_schedule(profile, rules, interval, now):
    local = interval.start.astimezone(MOSCOW_TZ)
    end = interval.end.astimezone(MOSCOW_TZ)
    if interval.start < now + timedelta(hours=profile.min_booking_notice_hours):
        raise BookingPolicyError("NOTICE_REQUIRED")
    today = now.astimezone(MOSCOW_TZ).date()
    if not today <= local.date() <= today + timedelta(days=profile.booking_horizon_days):
        raise BookingPolicyError("OUTSIDE_HORIZON")
    for start, finish in schedule_windows(rules, local.date()):
        if start <= local and end <= finish and (local-start).total_seconds() % 3600 == 0:
            return
    raise BookingPolicyError("OUTSIDE_SCHEDULE")


def scheduled_intervals(profile, rules, day, now, *, duration_minutes=None):
    today = now.astimezone(MOSCOW_TZ).date()
    if not today <= day <= today + timedelta(days=profile.booking_horizon_days):
        return ()
    duration = timedelta(minutes=duration_minutes if duration_minutes is not None else profile.default_duration)
    if duration <= timedelta(0):
        raise BookingPolicyError("INVALID_STATE")
    result = []
    for start, finish in schedule_windows(rules, day):
        while start + duration <= finish:
            interval = Interval(start, start+duration)
            if interval.start >= now + timedelta(hours=profile.min_booking_notice_hours):
                result.append(interval)
            start += timedelta(hours=1)
    return tuple(result)


def check_local(profile, interval, occupied):
    if any(interval.conflicts(other, profile.buffer_minutes) for other in occupied):
        raise BookingPolicyError("SLOT_UNAVAILABLE")
    day = interval.start.astimezone(MOSCOW_TZ).date()
    if sum(other.start.astimezone(MOSCOW_TZ).date() == day for other in occupied) >= profile.daily_limit:
        raise BookingPolicyError("DAILY_LIMIT_REACHED")
