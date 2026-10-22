"""Delivery adapters for booking decisions, rescheduling and cancellation.

Registered by the opt-in shared runtime. Tests replace all transports.
"""
import asyncio
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr

from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
    TelegramRetryAfter, TelegramServerError,
)
from sqlalchemy import select

from app.db.models import BookingRequest, CalendarSync, Meeting, OutboxEvent, OwnerProfile, User, RescheduleRequest
from app.domain.booking import format_moscow_dt
from app.services.email import _deliver_email, email_notifications_enabled
from app.services.outbox import DeliveryError, DeliveryResult
from app.domain.intervals import utc
from app.services.calendar_sync import enqueue_ready_notices


@dataclass(frozen=True)
class Notice:
    telegram_id: int
    email: str | None
    subject: str
    body: str
    code: str | None = None


class BookingDeliveries:
    def __init__(self, sessions, *, settings, bot, calendar_environment=None, clock=None):
        self.sessions = sessions
        self.settings = settings
        self.bot = bot
        self.calendar_environment = calendar_environment
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def handlers(self):
        return {(channel, kind): self.deliver
                for channel in ("telegram", "smtp")
                for kind in ("request.created", "request.approved", "request.calendar_ready", "request.rejected",
                    "meeting.reschedule_requested", "meeting.reschedule_rejected", "meeting.rescheduled",
                    "meeting.cancelled", "meeting.cancelled_owner", "meeting.calendar_ready")}

    async def _notice(self, delivery):
        # Read and close the session before calling a remote service.
        async with self.sessions() as session:
            if delivery.kind.startswith("meeting."):
                return await self._meeting_notice(session, delivery)
            request = await session.get(BookingRequest, delivery.entity_id)
            if request is None:
                raise DeliveryError("RESOURCE_NOT_FOUND", permanent=True)
            if request.version != delivery.entity_version:
                return DeliveryResult("superseded", "STALE_EVENT")
            client = await session.get(User, request.client_id)
            if client is None or client.status != "active":
                return DeliveryResult("skipped", "RECIPIENT_UNAVAILABLE")
            if delivery.kind == "request.rejected":
                if request.status != "rejected":
                    return DeliveryResult("superseded", "STALE_EVENT")
                return Notice(client.telegram_id, client.email, f"Заявка #{request.id} отклонена",
                    f"Ваша заявка #{request.id} отклонена.\nПричина: {request.rejection_reason or ''}")
            if delivery.kind == "request.created":
                if request.status != "pending":
                    return DeliveryResult("superseded", "STALE_EVENT")
                if delivery.channel == "smtp":
                    return DeliveryResult("skipped", "NOT_APPLICABLE")
                return Notice(self.settings.owner_telegram_id, None,
                    f"Новая заявка #{request.id}",
                    f"Новая заявка #{request.id}\nКлиент: {client.full_name}\n"
                    f"Время: {format_moscow_dt(request.start_at)} МСК\nТема: {request.title}")
            if delivery.kind not in ("request.approved", "request.calendar_ready"):
                raise DeliveryError("UNSUPPORTED_DELIVERY", permanent=True)
            meeting = await session.scalar(select(Meeting).where(Meeting.booking_request_id == request.id))
            if request.status != "approved" or meeting is None:
                return DeliveryResult("superseded", "STALE_EVENT")
            if meeting.status not in ("confirmed", "rescheduled") or meeting.version != 1:
                return DeliveryResult("superseded", "STALE_EVENT")
            profile = await session.scalar(select(OwnerProfile).join(User).where(
                User.telegram_id == self.settings.owner_telegram_id))
            if profile is None:
                raise DeliveryError("OWNER_UNAVAILABLE", permanent=True)
            if profile.calendar_id or profile.google_status == "active":
                sync = await session.get(CalendarSync, meeting.id)
                ready = (sync and self.calendar_environment and sync.environment == self.calendar_environment
                         and sync.calendar_id == profile.calendar_id and sync.state == "synced"
                         and sync.applied_version == meeting.version == sync.desired_version
                         and meeting.google_meet_url)
                if not ready:
                    item = await session.get(OutboxEvent, delivery.id)
                    delayed = item and utc(self.clock())-utc(item.created_at) >= timedelta(minutes=10)
                    if delivery.kind == "request.approved" and delayed:
                        return Notice(client.telegram_id, client.email,
                            f"Решение по заявке #{request.id} принято",
                            f"Ваша заявка #{request.id} согласована.\n"
                            f"Время: {format_moscow_dt(meeting.start_at)} МСК\n"
                            "Обновление Google Calendar и ссылка на встречу задерживаются. "
                            "Ссылку отправим отдельным сообщением.", "CALENDAR_DELAY_NOTICE")
                    raise DeliveryError("CALENDAR_NOT_SYNCED")
            elif delivery.kind == "request.calendar_ready":
                return DeliveryResult("superseded", "CALENDAR_DISCONNECTED")
            links = ""
            if meeting.google_meet_url:
                links += f"\nGoogle Meet: {meeting.google_meet_url}"
            if meeting.google_event_url:
                links += f"\nGoogle Calendar: {meeting.google_event_url}"
            return Notice(client.telegram_id, client.email,
                f"Заявка #{request.id} подтверждена",
                f"Ваша заявка #{request.id} подтверждена.\n"
                f"Время: {format_moscow_dt(meeting.start_at)} МСК\nТема: {meeting.title}{links}")

    async def _meeting_notice(self, session, delivery):
        meeting = await session.get(Meeting, delivery.entity_id)
        if meeting is None:
            raise DeliveryError("RESOURCE_NOT_FOUND", permanent=True)
        if meeting.version != delivery.entity_version:
            return DeliveryResult("superseded", "STALE_EVENT")
        client = await session.get(User, meeting.client_id)
        if client is None or (client.status != "active" and delivery.kind != "meeting.cancelled_owner"):
            return DeliveryResult("skipped", "RECIPIENT_UNAVAILABLE")
        when = format_moscow_dt(meeting.start_at)
        if delivery.kind == "meeting.cancelled_owner":
            if meeting.status != "cancelled_by_client" or delivery.channel != "telegram":
                return DeliveryResult("superseded", "STALE_EVENT")
            return Notice(self.settings.owner_telegram_id, None, f"Клиент отменил встречу #{meeting.id}",
                f"Клиент {client.full_name} отменил встречу #{meeting.id} на {when} МСК.\n"
                f"Причина: {meeting.cancellation_reason or 'не указана'}")
        if delivery.kind == "meeting.reschedule_requested":
            proposal = await session.scalar(select(RescheduleRequest).where(
                RescheduleRequest.meeting_id == meeting.id, RescheduleRequest.status == "pending"))
            if meeting.status != "reschedule_pending" or proposal is None:
                return DeliveryResult("superseded", "STALE_EVENT")
            if delivery.channel == "smtp":
                return DeliveryResult("skipped", "NOT_APPLICABLE")
            return Notice(self.settings.owner_telegram_id, None, f"Запрос переноса встречи #{meeting.id}",
                f"Клиент {client.full_name} просит перенести встречу #{meeting.id}.\n"
                f"Сейчас: {when} МСК\nПредложено: {format_moscow_dt(proposal.proposed_start_at)} МСК\n"
                "Откройте «Новые заявки», чтобы согласовать или отклонить перенос.")
        if delivery.kind == "meeting.reschedule_rejected":
            proposal = await session.scalar(select(RescheduleRequest).where(
                RescheduleRequest.meeting_id == meeting.id, RescheduleRequest.status == "rejected"
            ).order_by(RescheduleRequest.id.desc()))
            if proposal is None or meeting.status not in ("confirmed", "rescheduled"):
                return DeliveryResult("superseded", "STALE_EVENT")
            return Notice(client.telegram_id, client.email, f"Перенос встречи #{meeting.id} отклонён",
                f"Запрос переноса встречи #{meeting.id} отклонён.\n"
                f"Время не изменилось: {when} МСК\nПричина: {proposal.reason or ''}")
        sync = await session.get(CalendarSync, meeting.id)
        profile = await session.scalar(select(OwnerProfile).join(User).where(
            User.telegram_id == self.settings.owner_telegram_id))
        if profile is None:
            raise DeliveryError("OWNER_UNAVAILABLE", permanent=True)
        connected = bool(profile.calendar_id or profile.google_status == "active" or sync)
        ready = (sync and self.calendar_environment == sync.environment and profile.calendar_id == sync.calendar_id
                 and sync.state == "synced" and sync.applied_version == meeting.version == sync.desired_version)
        if delivery.kind == "meeting.cancelled":
            if not meeting.status.startswith("cancelled"):
                return DeliveryResult("superseded", "STALE_EVENT")
            pending = "\nОбновление Google Calendar ещё выполняется." if connected and not ready else ""
            return Notice(client.telegram_id, client.email, f"Встреча #{meeting.id} отменена",
                f"Встреча #{meeting.id} на {when} МСК отменена.\n"
                f"Причина: {meeting.cancellation_reason or 'не указана'}{pending}")
        if meeting.status != "rescheduled" or delivery.kind not in ("meeting.rescheduled", "meeting.calendar_ready"):
            return DeliveryResult("superseded", "STALE_EVENT")
        if connected and (not ready or not meeting.google_meet_url):
            item = await session.get(OutboxEvent, delivery.id)
            if (delivery.kind == "meeting.rescheduled" and item
                    and utc(self.clock())-utc(item.created_at) >= timedelta(minutes=10)):
                return Notice(client.telegram_id, client.email, f"Встреча #{meeting.id} перенесена",
                    f"Новое время встречи #{meeting.id}: {when} МСК.\n"
                    "Обновление Google Calendar задерживается. Ссылку отправим отдельно.", "CALENDAR_DELAY_NOTICE")
            raise DeliveryError("CALENDAR_NOT_SYNCED")
        if not connected and delivery.kind == "meeting.calendar_ready":
            return DeliveryResult("superseded", "CALENDAR_DISCONNECTED")
        link = f"\nGoogle Meet: {meeting.google_meet_url}" if meeting.google_meet_url else ""
        return Notice(client.telegram_id, client.email, f"Встреча #{meeting.id} перенесена",
            f"Новое время встречи #{meeting.id}: {when} МСК{link}")

    def _delivered(self, delivery, notice):
        async def follow_up(session):
            if delivery.kind.startswith("meeting."):
                meeting = await session.get(Meeting, delivery.entity_id)
            else:
                request = await session.get(BookingRequest, delivery.entity_id)
                if request is None:
                    return
                meeting = await session.scalar(select(Meeting).where(Meeting.booking_request_id == request.id))
            sync = await session.get(CalendarSync, meeting.id) if meeting else None
            if (sync and sync.state == "synced" and sync.applied_version == meeting.version
                    and sync.desired_version == meeting.version and meeting.google_meet_url):
                # Covers calendar finishing while the delay notice was in flight.
                await enqueue_ready_notices(session, meeting, utc(self.clock()))
        return DeliveryResult(code=notice.code,
                              on_commit=follow_up if notice.code == "CALENDAR_DELAY_NOTICE" else None)

    async def deliver(self, delivery):
        notice = await self._notice(delivery)
        if isinstance(notice, DeliveryResult):
            return notice
        if delivery.channel == "telegram":
            try:
                await self.bot.send_message(notice.telegram_id, notice.body, parse_mode=None)
            except TelegramRetryAfter as error:
                raise DeliveryError("TELEGRAM_RATE_LIMITED", retry_after=error.retry_after) from None
            except (TelegramForbiddenError, TelegramBadRequest):
                raise DeliveryError("TELEGRAM_REJECTED", permanent=True) from None
            except (TelegramNetworkError, TelegramServerError):
                raise DeliveryError("TELEGRAM_UNAVAILABLE") from None
            return self._delivered(delivery, notice)
        if delivery.channel != "smtp":
            raise DeliveryError("UNSUPPORTED_DELIVERY", permanent=True)
        if not notice.email:
            return DeliveryResult("skipped", "NO_EMAIL")
        if not email_notifications_enabled(self.settings):
            return DeliveryResult("skipped", "SMTP_DISABLED")
        message = EmailMessage()
        message["Message-ID"] = delivery.message_id
        message["From"] = formataddr((self.settings.smtp_from_name, self.settings.smtp_from_email))
        message["To"] = notice.email
        message["Subject"] = notice.subject
        message.set_content(notice.body)
        try:
            await asyncio.to_thread(_deliver_email, self.settings, message)
        except smtplib.SMTPResponseException as error:
            code = int(error.smtp_code)
            raise DeliveryError("SMTP_REJECTED", permanent=500 <= code < 600) from None
        except smtplib.SMTPRecipientsRefused as error:
            # A recipient-specific 4xx is temporary; do not persist server text/address.
            permanent = all(int(value[0]) >= 500 for value in error.recipients.values())
            raise DeliveryError("SMTP_RECIPIENT_REJECTED", permanent=permanent) from None
        except (OSError, smtplib.SMTPException):
            raise DeliveryError("SMTP_UNAVAILABLE") from None
        return self._delivered(delivery, notice)
