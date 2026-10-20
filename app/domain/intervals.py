"""UTC interval rules shared by future bot and HTTP commands."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def utc(value: datetime) -> datetime:
    """Persisted naive SQLite timestamps are UTC, never device local time."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self):
        object.__setattr__(self, "start", utc(self.start))
        object.__setattr__(self, "end", utc(self.end))
        if self.end <= self.start:
            raise ValueError("Interval end must be after start")

    def conflicts(self, other: "Interval", buffer_minutes: int = 0) -> bool:
        if buffer_minutes < 0:
            raise ValueError("buffer_minutes must be non-negative")
        gap = timedelta(minutes=buffer_minutes)
        return not (self.end + gap <= other.start or other.end + gap <= self.start)
