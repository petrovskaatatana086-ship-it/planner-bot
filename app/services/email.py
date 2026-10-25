import asyncio
import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, getaddresses

from app.config import Settings
from app.db.models import BookingRequest, Meeting, User
from app.domain.booking import format_moscow_dt

logger = logging.getLogger(__name__)


def email_notifications_enabled(settings: Settings) -> bool:
    return bool(
        settings.smtp_host
        and settings.smtp_from_email
        and settings.smtp_username
        and settings.smtp_password
    )


async def send_booking_approved_email(
    settings: Settings,
    *,
    client: User,
    request: BookingRequest,
    meeting: Meeting,
) -> bool:
    calendar_line = f"\nСсылка на событие в Google Calendar: {meeting.google_event_url}" if meeting.google_event_url else ""
    meet_line = f"\nСсылка Google Meet: {meeting.google_meet_url}" if meeting.google_meet_url else ""
    return await _send_email(
        settings,
        to_email=client.email,
        subject=f"Заявка #{request.id} подтверждена",
        body=(
            f"Здравствуйте, {client.full_name}!\n\n"
            f"Ваша заявка #{request.id} подтверждена.\n"
            f"Дата и время: {format_moscow_dt(meeting.start_at)} МСК\n"
            f"Тема: {meeting.title}"
            f"{calendar_line}"
            f"{meet_line}\n\n"
            "Если планы изменятся, пожалуйста, сообщите заранее."
        ),
    )


async def send_booking_rejected_email(
    settings: Settings,
    *,
    client: User,
    request: BookingRequest,
    reason: str,
) -> bool:
    return await _send_email(
        settings,
        to_email=client.email,
        subject=f"Заявка #{request.id} отклонена",
        body=(
            f"Здравствуйте, {client.full_name}!\n\n"
            f"Ваша заявка #{request.id} на {format_moscow_dt(request.start_at)} МСК отклонена.\n"
            f"Причина: {reason}\n\n"
            "Вы можете выбрать другое время и отправить новую заявку."
        ),
    )


async def send_meeting_rescheduled_email(
    settings: Settings,
    *,
    client: User,
    meeting: Meeting,
    reason: str | None = None,
) -> bool:
    reason_line = f"\nКомментарий: {reason}" if reason else ""
    calendar_line = f"\nСсылка на событие в Google Calendar: {meeting.google_event_url}" if meeting.google_event_url else ""
    meet_line = f"\nСсылка Google Meet: {meeting.google_meet_url}" if meeting.google_meet_url else ""
    return await _send_email(
        settings,
        to_email=client.email,
        subject=f"Встреча #{meeting.id} перенесена",
        body=(
            f"Здравствуйте, {client.full_name}!\n\n"
            f"Встреча #{meeting.id} перенесена.\n"
            f"Новое время: {format_moscow_dt(meeting.start_at)} МСК\n"
            f"Тема: {meeting.title}"
            f"{calendar_line}"
            f"{meet_line}"
            f"{reason_line}\n\n"
            "Пожалуйста, проверьте, что новое время вам подходит."
        ),
    )


async def send_meeting_reschedule_rejected_email(
    settings: Settings,
    *,
    client: User,
    meeting: Meeting,
    reason: str,
) -> bool:
    return await _send_email(
        settings,
        to_email=client.email,
        subject=f"Запрос на перенос встречи #{meeting.id} отклонен",
        body=(
            f"Здравствуйте, {client.full_name}!\n\n"
            f"Запрос на перенос встречи #{meeting.id} отклонен.\n"
            f"Текущая дата и время встречи: {format_moscow_dt(meeting.start_at)} МСК\n"
            f"Причина: {reason}\n\n"
            "Если нужно, вы можете обсудить другое время или отменить встречу заранее."
        ),
    )


async def send_meeting_cancelled_email(
    settings: Settings,
    *,
    client: User,
    meeting: Meeting,
    reason: str,
) -> bool:
    return await _send_email(
        settings,
        to_email=client.email,
        subject=f"Встреча #{meeting.id} отменена",
        body=(
            f"Здравствуйте, {client.full_name}!\n\n"
            f"Встреча #{meeting.id}, назначенная на {format_moscow_dt(meeting.start_at)} МСК, отменена.\n"
            f"Причина: {reason}\n\n"
            "Если нужно, вы можете оформить новую запись на другое время."
        ),
    )


async def _send_email(
    settings: Settings,
    *,
    to_email: str | None,
    subject: str,
    body: str,
) -> bool:
    if not to_email:
        return False
    if not email_notifications_enabled(settings):
        logger.info("Email notifications skipped: SMTP is not configured")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.smtp_from_name, settings.smtp_from_email))
    message["To"] = to_email
    message.set_content(body)

    try:
        await asyncio.to_thread(_deliver_email, settings, message)
    except Exception:
        logger.exception("Failed to send email notification to %s", to_email)
        return False
    return True


def _deliver_email(settings: Settings, message: EmailMessage) -> None:
    admin_copy = getattr(settings, "smtp_admin_copy_email", "").strip()
    recipients = {address.casefold() for _, address in getaddresses(
        message.get_all("To", []) + message.get_all("Cc", []) + message.get_all("Bcc", [])
    )}
    if admin_copy and admin_copy.casefold() not in recipients:
        # SMTP sends Bcc recipients in the envelope and removes the header on the wire.
        if message.get_all("Bcc"):
            message.replace_header("Bcc", message["Bcc"] + ", " + admin_copy)
        else:
            message["Bcc"] = admin_copy
    if settings.smtp_use_ssl:
        server: smtplib.SMTP | smtplib.SMTP_SSL = smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=30,
        )
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)

    with server:
        server.ehlo()
        if settings.smtp_use_tls and not settings.smtp_use_ssl:
            server.starttls()
            server.ehlo()
        server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)
