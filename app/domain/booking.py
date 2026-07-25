from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
MEETING_DURATION_MINUTES = 60
BOOKING_HORIZON_DAYS = 14
MIN_BOOKING_NOTICE_HOURS = 24
WORK_START = time(10, 0)
WORK_END = time(18, 0)
WORKDAYS = {0, 1, 2, 3, 4}


def to_moscow(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW_TZ)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=MOSCOW_TZ)
    return value.astimezone(timezone.utc)


def format_moscow_dt(value: datetime) -> str:
    local = to_moscow(value)
    return local.strftime("%d.%m.%Y %H:%M")


def format_moscow_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def parse_moscow_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def parse_moscow_time(value: str) -> time | None:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError:
        return None


def available_dates(
    now: datetime | None = None,
    *,
    horizon_days: int = BOOKING_HORIZON_DAYS,
    duration_minutes: int = MEETING_DURATION_MINUTES,
    min_notice_hours: int = MIN_BOOKING_NOTICE_HOURS,
) -> list[date]:
    current = to_moscow(now or datetime.now(timezone.utc))
    result: list[date] = []
    for offset in range(horizon_days + 1):
        candidate = current.date() + timedelta(days=offset)
        if candidate.weekday() not in WORKDAYS:
            continue
        if available_slots_for_date(
            candidate,
            now=current,
            duration_minutes=duration_minutes,
            min_notice_hours=min_notice_hours,
        ):
            result.append(candidate)
    return result


def available_slots_for_date(
    selected_date: date,
    *,
    busy_starts: set[datetime] | None = None,
    now: datetime | None = None,
    duration_minutes: int = MEETING_DURATION_MINUTES,
    min_notice_hours: int = MIN_BOOKING_NOTICE_HOURS,
) -> list[datetime]:
    busy_starts = busy_starts or set()
    current = to_moscow(now or datetime.now(timezone.utc))
    min_start = current + timedelta(hours=min_notice_hours)

    start = datetime.combine(selected_date, WORK_START, tzinfo=MOSCOW_TZ)
    end_of_day = datetime.combine(selected_date, WORK_END, tzinfo=MOSCOW_TZ)
    slots: list[datetime] = []

    while start + timedelta(minutes=duration_minutes) <= end_of_day:
        start_utc = to_utc(start)
        if start >= min_start and start_utc not in busy_starts:
            slots.append(start)
        start += timedelta(minutes=MEETING_DURATION_MINUTES)

    return slots


def validate_title(value: str) -> str | None:
    title = value.strip()
    if len(title) < 3:
        return None
    if len(title) > 100:
        return None
    return title
