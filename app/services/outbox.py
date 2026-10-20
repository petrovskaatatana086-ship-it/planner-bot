"""Durable delivery engine. Network adapters execute outside write transactions.

Lease fencing protects DB acknowledgements, not exactly-once remote delivery.
A crash after the remote server accepts a message can still cause a duplicate.
The worker is intentionally not started by app.main until bot migration is done.
"""
import asyncio
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.db.models import OutboxEvent
from app.db.transactions import DatabaseBusy, run_sqlite_write
from app.domain.intervals import utc

RETRY_DELAYS = (5, 30, 120, 600, 1800)
TERMINAL = ("sent", "skipped", "superseded")


def safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value) else "DELIVERY_ERROR"


class DeliveryError(Exception):
    def __init__(self, code, *, permanent=False, retry_after=None, on_commit=None):
        self.code = safe_code(code)
        self.permanent = permanent
        self.retry_after = retry_after
        self.on_commit = on_commit
        # Never put external exception messages into logs/storage.
        super().__init__(self.code)


@dataclass(frozen=True)
class Delivery:
    id: int
    event_id: str
    entity_id: int
    entity_version: int
    channel: str
    kind: str
    attempts: int
    lease_token: str

    @property
    def message_id(self):
        return f"<{self.event_id}.{self.channel}@meetly.invalid>"


@dataclass(frozen=True)
class DeliveryResult:
    state: str = "sent"
    code: str | None = None
    on_commit: Callable | None = None

    def __post_init__(self):
        if self.state not in TERMINAL:
            raise ValueError("Delivery result must be terminal")


Handler = Callable[[Delivery], Awaitable[DeliveryResult]]


class OutboxWorker:
    def __init__(self, sessions, handlers: Mapping[tuple[str, str], Handler], *,
                 clock=None, lease_seconds=90, timeout_seconds=30):
        if not 0 < timeout_seconds < lease_seconds:
            raise ValueError("Network timeout must be positive and shorter than lease")
        self.sessions = sessions
        self.handlers = dict(handlers)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds = lease_seconds
        self.timeout_seconds = timeout_seconds

    async def claim(self):
        async def acquire(session):
            now = utc(self.clock())
            previous = aliased(OutboxEvent)
            family = lambda row: func.substr(row.kind, 1, func.instr(row.kind, ".") - 1)
            predecessor = exists(select(previous.id).where(
                previous.id < OutboxEvent.id,
                previous.entity_id == OutboxEvent.entity_id,
                previous.channel == OutboxEvent.channel,
                family(previous) == family(OutboxEvent),
                or_(previous.state.not_in((*TERMINAL, "failed")),
                    and_(previous.channel == "google", previous.state == "failed")),
            ))
            due = or_(
                and_(OutboxEvent.state.in_(("pending", "retry")), OutboxEvent.next_attempt_at <= now),
                and_(OutboxEvent.state == "processing",
                     or_(OutboxEvent.lease_until <= now, OutboxEvent.lease_until.is_(None))),
            )
            # Exhausted abandoned leases are marked failed before considering other entities.
            while True:
                item = await session.scalar(select(OutboxEvent).where(due, ~predecessor)
                                            .order_by(OutboxEvent.id).limit(1))
                if item is None:
                    return None
                if item.attempts >= len(RETRY_DELAYS) + 1:
                    item.state = "failed"
                    item.error_code = "ATTEMPTS_EXHAUSTED"
                    item.lease_token = None
                    item.lease_until = None
                    await session.flush()
                    continue
                token = uuid.uuid4().hex
                item.state = "processing"
                item.attempts += 1
                item.lease_token = token
                item.lease_until = now + timedelta(seconds=self.lease_seconds)
                item.error_code = None
                await session.flush()
                return Delivery(item.id, item.event_id, item.entity_id, item.entity_version,
                                item.channel, item.kind, item.attempts, token)
        return await run_sqlite_write(self.sessions, acquire)

    async def finish(self, delivery, *, result=None, error=None):
        if (result is None) == (error is None):
            raise ValueError("Provide exactly one result or error")

        async def acknowledge(session):
            now = utc(self.clock())
            item = await session.get(OutboxEvent, delivery.id)
            if (item is None or item.state != "processing" or item.lease_token != delivery.lease_token
                    or item.lease_until is None or utc(item.lease_until) <= now):
                return False
            item.lease_token = None
            item.lease_until = None
            if result is not None:
                item.state = result.state
                item.error_code = safe_code(result.code) if result.code else None
            else:
                item.error_code = error.code
                if error.permanent or item.attempts > len(RETRY_DELAYS):
                    item.state = "failed"
                else:
                    item.state = "retry"
                    delay = RETRY_DELAYS[item.attempts - 1]
                    extra = error.retry_after
                    if isinstance(extra, (int, float)) and math.isfinite(extra) and extra > 0:
                        delay = max(delay, min(extra, 86400))
                    item.next_attempt_at = now + timedelta(seconds=delay)
            hook = result.on_commit if result is not None else error.on_commit
            if hook is not None:
                # DB-only effect, fenced by the lease and committed with the acknowledgement.
                await hook(session)
            return True
        return await run_sqlite_write(self.sessions, acknowledge)

    async def retry_failed(self, identifier):
        """Internal operation; future HTTP adapter must enforce owner/version/key."""
        async def reset(session):
            item = await session.get(OutboxEvent, identifier)
            if item is None or item.state != "failed":
                return False
            item.state = "retry"
            item.attempts = 0
            item.next_attempt_at = utc(self.clock())
            item.error_code = None
            item.lease_token = None
            item.lease_until = None
            return True
        return await run_sqlite_write(self.sessions, reset)

    async def run_once(self):
        delivery = await self.claim()
        if delivery is None:
            return False
        handler = self.handlers.get((delivery.channel, delivery.kind))
        try:
            if handler is None:
                raise DeliveryError("UNSUPPORTED_DELIVERY", permanent=True)
            result = await asyncio.wait_for(handler(delivery), timeout=self.timeout_seconds)
            if not isinstance(result, DeliveryResult):
                raise DeliveryError("INVALID_DELIVERY_RESULT", permanent=True)
        except asyncio.CancelledError:
            # Outcome may be ambiguous; keep lease until expiry, do not immediately resend.
            raise
        except DeliveryError as error:
            await self.finish(delivery, error=error)
        except TimeoutError:
            await self.finish(delivery, error=DeliveryError("DELIVERY_TIMEOUT"))
        except Exception:
            await self.finish(delivery, error=DeliveryError("DELIVERY_ERROR"))
        else:
            await self.finish(delivery, result=result)
        return True

    async def run(self, stop: asyncio.Event, *, poll_seconds=1):
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except DatabaseBusy:
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
