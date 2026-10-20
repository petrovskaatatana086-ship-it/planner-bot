"""Delivery adapters for the two booking commands currently implemented.

No adapter is registered in app.main yet. Tests replace all network transports.
"""
import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
    TelegramRetryAfter, TelegramServerError,
)
from sqlalchemy import select

from app.db.models import BookingRequest, Meeting, OwnerProfile, User
from app.domain.booking import format_moscow_dt
from app.services.email import _deliver_email, email_notifications_enabled
from app.services.outbox import DeliveryError, DeliveryResult


@dataclass(frozen=True)
class Notice:
    telegram_id: int
    email: str | None
    subject: str
    body: str


class BookingDeliveries:
    def __init__(self, sessions, *, settings, bot):
        self.sessions = sessions
        self.settings = settings
        self.bot = bot

    def handlers(self):
        return {(channel, kind): self.deliver
                for channel in ("telegram", "smtp")
                for kind in ("request.created", "request.approved")}

    async def _notice(self, delivery):
        # Read and close the session before calling a remote service.
        async with self.sessions() as session:
            request = await session.get(BookingRequest, delivery.entity_id)
            if request is None:
                raise DeliveryError("RESOURCE_NOT_FOUND", permanent=True)
            if request.version != delivery.entity_version:
                return DeliveryResult("superseded", "STALE_EVENT")
            client = await session.get(User, request.client_id)
            if client is None or client.status != "active":
                return DeliveryResult("skipped", "RECIPIENT_UNAVAILABLE")
            if delivery.kind == "request.created":
                if request.status != "pending":
                    return DeliveryResult("superseded", "STALE_EVENT")
                if delivery.channel == "smtp":
                    return DeliveryResult("skipped", "NOT_APPLICABLE")
                return Notice(self.settings.owner_telegram_id, None,
                    f"Новая заявка #{request.id}",
                    f"Новая заявка #{request.id}\nКлиент: {client.full_name}\n"
                    f"Время: {format_moscow_dt(request.start_at)} МСК\nТема: {request.title}")
            if delivery.kind != "request.approved":
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
            # Calendar sync and its version table are the next implementation block.
            # Never report a final invite as delivered while a configured calendar waits.
            if profile.calendar_id or profile.google_status == "active":
                raise DeliveryError("CALENDAR_NOT_SYNCED")
            return Notice(client.telegram_id, client.email,
                f"Заявка #{request.id} подтверждена",
                f"Ваша заявка #{request.id} подтверждена.\n"
                f"Время: {format_moscow_dt(meeting.start_at)} МСК\nТема: {meeting.title}")

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
            return DeliveryResult()
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
        return DeliveryResult()
