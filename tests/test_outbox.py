"""Crash/retry/concurrency tests. All delivery transports are fakes."""
import asyncio
import smtplib
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.base import Base
from app.db.models import OutboxEvent, User, OwnerProfile, BookingRequest, Meeting
from app.db.session import make_engine
from app.db.transactions import run_sqlite_write
from app.services.booking_deliveries import BookingDeliveries
from app.services.outbox import DeliveryError, DeliveryResult, OutboxWorker


class OutboxTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path.cwd() / f".test_outbox_{uuid.uuid4().hex}.db"
        self.settings = Settings(_env_file=None, BOT_TOKEN="123:abc", OWNER_TELEGRAM_ID=777,
            DATABASE_URL=f"sqlite+aiosqlite:///{self.path.as_posix()}")
        self.engines = [make_engine(self.settings), make_engine(self.settings)]
        self.sessions = [async_sessionmaker(e, expire_on_commit=False) for e in self.engines]
        async with self.engines[0].begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.now = datetime(2026, 10, 21, 7, tzinfo=timezone.utc)
        self.delivered = []
        async def send(delivery):
            self.delivered.append(delivery.id)
            return DeliveryResult()
        self.handlers = {("telegram", "request.created"): send}
        self.workers = [OutboxWorker(s, self.handlers, clock=lambda: self.now) for s in self.sessions]

    async def asyncTearDown(self):
        for engine in self.engines:
            await engine.dispose()
        self.path.unlink(missing_ok=True)

    async def seed(self, *, entity=1, channel="telegram", kind="request.created", **kwargs):
        async with self.sessions[0]() as session:
            event = OutboxEvent(event_id=uuid.uuid4().hex, entity_id=entity, entity_version=1,
                channel=channel, kind=kind, state="pending", attempts=0,
                next_attempt_at=self.now, created_at=self.now, **kwargs)
            session.add(event)
            await session.commit()
            return event.id

    async def row(self, identifier):
        async with self.sessions[0]() as session:
            return await session.get(OutboxEvent, identifier)

    async def test_two_workers_send_once(self):
        identifier = await self.seed()
        await asyncio.gather(*(w.run_once() for w in self.workers))
        self.assertEqual(self.delivered, [identifier])
        self.assertEqual((await self.row(identifier)).state, "sent")
        self.assertFalse(await self.workers[0].run_once())

    async def test_recover_after_crash_and_fence_old_worker(self):
        identifier = await self.seed()
        old = await self.workers[0].claim()
        self.assertIsNone(await self.workers[1].claim())
        self.now += timedelta(seconds=90)
        new = await self.workers[1].claim()
        self.assertEqual(old.id, new.id)
        self.assertNotEqual(old.lease_token, new.lease_token)
        self.assertFalse(await self.workers[0].finish(old, result=DeliveryResult()))
        self.assertTrue(await self.workers[1].finish(new, result=DeliveryResult()))
        self.assertEqual((await self.row(identifier)).attempts, 2)

    async def test_expired_ack_without_reclaim_is_rejected(self):
        identifier = await self.seed()
        delivery = await self.workers[0].claim()
        self.now += timedelta(seconds=91)
        self.assertFalse(await self.workers[0].finish(delivery, result=DeliveryResult()))
        self.assertEqual((await self.row(identifier)).state, "processing")

    async def test_transient_retry_after_and_sanitized_error(self):
        identifier = await self.seed()
        async def rate_limit(_):
            raise DeliveryError("UNSAFE secret@example.com", retry_after=60)
        self.workers[0].handlers[("telegram", "request.created")] = rate_limit
        await self.workers[0].run_once()
        event = await self.row(identifier)
        self.assertEqual(event.state, "retry")
        self.assertEqual(event.error_code, "DELIVERY_ERROR")
        self.now += timedelta(seconds=59)
        self.assertIsNone(await self.workers[0].claim())
        self.now += timedelta(seconds=1)
        self.assertIsNotNone(await self.workers[0].claim())

    async def test_max_attempts_and_manual_retry_same_id(self):
        identifier = await self.seed()
        async def unavailable(_):
            raise DeliveryError("TEMPORARY")
        self.workers[0].handlers[("telegram", "request.created")] = unavailable
        for _ in range(6):
            self.assertTrue(await self.workers[0].run_once())
            self.now += timedelta(hours=1)
        self.assertEqual((await self.row(identifier)).state, "failed")
        self.assertFalse(await self.workers[0].run_once())
        self.assertTrue(await self.workers[0].retry_failed(identifier))
        self.workers[0].handlers.update(self.handlers)
        await self.workers[0].run_once()
        self.assertEqual((await self.row(identifier)).state, "sent")
        self.assertEqual((await self.row(identifier)).attempts, 1)

    async def test_exhausted_abandoned_claim_not_delivered_again(self):
        identifier = await self.seed()
        for _ in range(6):
            self.assertIsNotNone(await self.workers[0].claim())
            self.now += timedelta(seconds=91)
        self.assertIsNone(await self.workers[1].claim())
        self.assertEqual((await self.row(identifier)).state, "failed")
        self.assertEqual(self.delivered, [])

    async def test_fifo_per_channel_entity_and_family(self):
        first = await self.seed()
        second = await self.seed(kind="request.approved")
        other = await self.seed(entity=2)
        meeting = await self.seed(kind="meeting.cancelled")
        smtp = await self.seed(channel="smtp")
        lease = await self.workers[0].claim()
        self.assertEqual(lease.id, first)
        self.assertEqual((await self.workers[1].claim()).id, other)
        self.assertEqual((await self.workers[1].claim()).id, meeting)
        self.assertEqual((await self.workers[1].claim()).id, smtp)
        self.assertIsNone(await self.workers[1].claim())
        await self.workers[0].finish(lease, result=DeliveryResult())
        self.assertEqual((await self.workers[1].claim()).id, second)

    async def test_permanent_failure_blocks_later_same_entity_only(self):
        first = await self.seed(channel="google", kind="meeting.created")
        await self.seed(channel="google", kind="meeting.updated")
        other = await self.seed(entity=2)
        delivery = await self.workers[0].claim()
        await self.workers[0].finish(delivery, error=DeliveryError("REJECTED", permanent=True))
        self.assertEqual((await self.row(first)).state, "failed")
        self.assertEqual((await self.workers[1].claim()).id, other)
        self.assertIsNone(await self.workers[1].claim())

    async def test_failed_owner_notice_does_not_block_client_confirmation(self):
        await self.seed()
        confirmation = await self.seed(kind="request.approved")
        delivery = await self.workers[0].claim()
        await self.workers[0].finish(delivery, error=DeliveryError("RECIPIENT_BLOCKED", permanent=True))
        self.assertEqual((await self.workers[1].claim()).id, confirmation)

    async def test_retry_delay_sequence(self):
        identifier = await self.seed()
        for delay in (5, 30, 120, 600, 1800):
            delivery = await self.workers[0].claim()
            await self.workers[0].finish(delivery, error=DeliveryError("TEMPORARY"))
            event = await self.row(identifier)
            # SQLite strips tzinfo; compare the UTC timestamp explicitly.
            self.assertEqual(event.next_attempt_at.replace(tzinfo=timezone.utc),
                             self.now+timedelta(seconds=delay))
            self.now += timedelta(seconds=delay)

    async def test_crash_after_remote_acceptance_may_redeliver_same_message_id(self):
        await self.seed()
        accepted = []
        old = await self.workers[0].claim()
        accepted.append(old.message_id)  # remote accepts, process dies before DB ack
        self.now += timedelta(seconds=91)
        new = await self.workers[1].claim()
        accepted.append(new.message_id)
        await self.workers[1].finish(new, result=DeliveryResult())
        self.assertEqual(accepted[0], accepted[1])
        self.assertEqual(new.attempts, 2)

    async def test_cancelled_network_leaves_lease_for_recovery(self):
        identifier = await self.seed()
        async def cancelled(_):
            raise asyncio.CancelledError()
        self.workers[0].handlers[("telegram", "request.created")] = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.workers[0].run_once()
        self.assertEqual((await self.row(identifier)).state, "processing")
        self.now += timedelta(seconds=91)
        await self.workers[1].run_once()
        self.assertEqual((await self.row(identifier)).state, "sent")

    async def test_network_does_not_hold_database_write_lock(self):
        await self.seed()
        async def send(_):
            async def mutate(session):
                session.add(OutboxEvent(event_id=uuid.uuid4().hex, entity_id=99, entity_version=1,
                    channel="smtp", kind="request.created", state="pending", attempts=0,
                    next_attempt_at=self.now, created_at=self.now))
            await asyncio.wait_for(run_sqlite_write(self.sessions[1], mutate), timeout=1)
            return DeliveryResult()
        self.workers[0].handlers[("telegram", "request.created")] = send
        await self.workers[0].run_once()
        self.assertEqual((await self.row(1)).state, "sent")

    async def test_timeout_and_unknown_handler_are_visible(self):
        first = await self.seed()
        worker = OutboxWorker(self.sessions[0], {("telegram", "request.created"): AsyncMock(side_effect=TimeoutError)},
                              clock=lambda: self.now)
        await worker.run_once()
        self.assertEqual((await self.row(first)).error_code, "DELIVERY_TIMEOUT")
        other = await self.seed(entity=2, kind="unknown.created")
        await worker.run_once()
        self.assertEqual((await self.row(other)).state, "failed")
        self.assertEqual((await self.row(other)).error_code, "UNSUPPORTED_DELIVERY")

    async def test_stop_wait_is_interruptible(self):
        stop = asyncio.Event()
        task = asyncio.create_task(self.workers[0].run(stop, poll_seconds=30))
        await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    async def make_booking(self):
        async with self.sessions[0]() as session:
            owner = User(telegram_id=777, role="owner", full_name="Owner")
            client = User(telegram_id=101, role="client", full_name="Synthetic", email="test@example.com")
            session.add_all([owner, client])
            await session.flush()
            session.add(OwnerProfile(user_id=owner.id))
            request = BookingRequest(client_id=client.id, title="Synthetic", start_at=self.now+timedelta(days=3),
                end_at=self.now+timedelta(days=3, hours=1), version=2, status="approved")
            session.add(request)
            await session.flush()
            meeting = Meeting(booking_request_id=request.id, client_id=client.id, title=request.title,
                start_at=request.start_at, end_at=request.end_at, version=1, status="confirmed")
            session.add(meeting)
            event = OutboxEvent(event_id=uuid.uuid4().hex, entity_id=request.id, entity_version=2,
                kind="request.approved", channel="smtp", state="pending", attempts=0,
                next_attempt_at=self.now, created_at=self.now)
            session.add(event)
            await session.commit()
            return event.id

    async def test_smtp_stable_message_id_and_retry_no_raw_error(self):
        identifier = await self.make_booking()
        self.settings.smtp_host = "smtp.example.com"
        self.settings.smtp_username = "test"
        self.settings.smtp_password = "synthetic"
        self.settings.smtp_from_email = "test@example.com"
        adapters = BookingDeliveries(self.sessions[0], settings=self.settings, bot=AsyncMock(), clock=lambda: self.now)
        worker = OutboxWorker(self.sessions[0], adapters.handlers(), clock=lambda: self.now)
        with patch("app.services.booking_deliveries._deliver_email", side_effect=smtplib.SMTPDataError(451, b"private")) as smtp:
            await worker.run_once()
            first_id = smtp.call_args.args[1]["Message-ID"]
        self.assertEqual((await self.row(identifier)).state, "retry")
        self.assertEqual((await self.row(identifier)).error_code, "SMTP_REJECTED")
        self.now += timedelta(seconds=5)
        with patch("app.services.booking_deliveries._deliver_email") as smtp:
            await worker.run_once()
            self.assertEqual(smtp.call_args.args[1]["Message-ID"], first_id)
        self.assertEqual((await self.row(identifier)).state, "sent")

    async def test_disabled_smtp_is_explicit_skip(self):
        identifier = await self.make_booking()
        adapters = BookingDeliveries(self.sessions[0], settings=self.settings, bot=AsyncMock(), clock=lambda: self.now)
        with patch("app.services.booking_deliveries._deliver_email") as smtp:
            await OutboxWorker(self.sessions[0], adapters.handlers(), clock=lambda: self.now).run_once()
            smtp.assert_not_called()
        self.assertEqual((await self.row(identifier)).state, "skipped")
        self.assertEqual((await self.row(identifier)).error_code, "SMTP_DISABLED")

    async def test_telegram_adapter_sends_plain_text_to_client(self):
        identifier = await self.make_booking()
        async with self.sessions[0]() as session:
            event = await session.get(OutboxEvent, identifier)
            event.channel = "telegram"
            await session.commit()
        bot = AsyncMock()
        adapters = BookingDeliveries(self.sessions[0], settings=self.settings, bot=bot)
        await OutboxWorker(self.sessions[0], adapters.handlers(), clock=lambda: self.now).run_once()
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.args[0], 101)
        self.assertIsNone(bot.send_message.await_args.kwargs["parse_mode"])
        self.assertEqual((await self.row(identifier)).state, "sent")

    async def test_smtp_permanent_error_is_not_retried(self):
        identifier = await self.make_booking()
        self.settings.smtp_host = "smtp.example.com"
        self.settings.smtp_username = "test"
        self.settings.smtp_password = "synthetic"
        self.settings.smtp_from_email = "test@example.com"
        adapters = BookingDeliveries(self.sessions[0], settings=self.settings, bot=AsyncMock(), clock=lambda: self.now)
        worker = OutboxWorker(self.sessions[0], adapters.handlers(), clock=lambda: self.now)
        with patch("app.services.booking_deliveries._deliver_email", side_effect=smtplib.SMTPDataError(550, b"private")):
            await worker.run_once()
        self.assertEqual((await self.row(identifier)).state, "failed")
        self.assertFalse(await worker.run_once())

    async def test_actual_network_timeout_cancels_handler(self):
        identifier = await self.seed()
        cancelled = asyncio.Event()
        async def hanging(_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        worker = OutboxWorker(self.sessions[0], {("telegram", "request.created"): hanging},
                              clock=lambda: self.now, timeout_seconds=0.01)
        await worker.run_once()
        self.assertTrue(cancelled.is_set())
        self.assertEqual((await self.row(identifier)).error_code, "DELIVERY_TIMEOUT")

    async def test_stale_notice_is_superseded_and_calendar_never_bypassed(self):
        identifier = await self.make_booking()
        adapters = BookingDeliveries(self.sessions[0], settings=self.settings, bot=AsyncMock(), clock=lambda: self.now)
        worker = OutboxWorker(self.sessions[0], adapters.handlers(), clock=lambda: self.now)
        async with self.sessions[0]() as session:
            profile = await session.scalar(select(OwnerProfile))
            profile.calendar_id = "synthetic-calendar"
            await session.commit()
        await worker.run_once()
        self.assertEqual((await self.row(identifier)).error_code, "CALENDAR_NOT_SYNCED")
        async with self.sessions[0]() as session:
            request = await session.get(BookingRequest, 1)
            request.version += 1
            await session.commit()
        self.now += timedelta(seconds=5)
        await worker.run_once()
        self.assertEqual((await self.row(identifier)).state, "superseded")
