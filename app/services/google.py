from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import BookingRequest, Meeting, OwnerProfile, User
from app.db.repositories import (
    get_google_oauth_credential,
    get_owner_profile,
    update_google_access_token,
    update_meeting_google_data,
)
from app.domain.booking import MOSCOW_TZ, available_slots_for_date, to_utc
from app.integrations.google_calendar import (
    busy_periods_to_slot_starts,
    cancel_google_event,
    create_google_meeting_event,
    get_google_busy_periods,
    update_google_event_time,
)
from app.integrations.google_oauth import refresh_access_token
from app.security import decrypt_secret, encrypt_secret

TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


def _build_google_event_description(request: BookingRequest, client: User) -> str:
    lines = [
        f"Название встречи: {request.title}",
        f"Комментарий пользователя: {request.comment or 'нет'}",
        f"Email пользователя: {client.email or 'не указан'}",
        f"Telegram display name: {client.full_name}",
        f"Telegram username: @{client.username}" if client.username else "Telegram username: не указан",
        "Служебная пометка: событие создано через Telegram-бота.",
    ]
    return "\n".join(lines)


async def get_valid_google_access_token(
    session: AsyncSession,
    *,
    settings: Settings,
    owner_user_id: int,
) -> str:
    credential = await get_google_oauth_credential(session, owner_user_id)
    if credential is None or credential.status != "active":
        raise ValueError("Google Calendar is not connected")
    if not credential.encrypted_access_token:
        raise ValueError("Google access token is missing")
    if not settings.oauth_encryption_key:
        raise ValueError("OAUTH_ENCRYPTION_KEY is not configured")

    expires_at = credential.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at is None or expires_at > datetime.now(timezone.utc) + TOKEN_REFRESH_MARGIN:
        return decrypt_secret(credential.encrypted_access_token, settings.oauth_encryption_key)

    if not credential.encrypted_refresh_token:
        raise ValueError("Google refresh token is missing")

    refresh_token = decrypt_secret(credential.encrypted_refresh_token, settings.oauth_encryption_key)
    refreshed = await refresh_access_token(settings, refresh_token)
    encrypted_access_token = encrypt_secret(refreshed.access_token, settings.oauth_encryption_key)
    await update_google_access_token(
        session,
        owner_user_id=owner_user_id,
        encrypted_access_token=encrypted_access_token,
        expires_at=refreshed.expires_at,
    )
    return refreshed.access_token


async def get_google_busy_starts_for_date(
    session: AsyncSession,
    *,
    settings: Settings,
    owner_user_id: int,
    selected_date: date,
    duration_minutes: int,
    min_notice_hours: int,
) -> set[datetime]:
    profile = await get_owner_profile(session, owner_user_id)
    if not _google_calendar_ready(profile):
        return set()

    access_token = await get_valid_google_access_token(
        session,
        settings=settings,
        owner_user_id=owner_user_id,
    )
    all_slots = available_slots_for_date(
        selected_date,
        duration_minutes=duration_minutes,
        min_notice_hours=min_notice_hours,
    )
    if not all_slots:
        return set()

    day_start = datetime.combine(selected_date, time(0, 0), tzinfo=MOSCOW_TZ)
    day_end = day_start + timedelta(days=1)
    busy_periods = await get_google_busy_periods(
        access_token,
        calendar_id=profile.calendar_id,
        time_min=to_utc(day_start),
        time_max=to_utc(day_end),
    )
    busy_local_starts = busy_periods_to_slot_starts(
        busy_periods,
        all_slots,
        duration_minutes=duration_minutes,
    )
    return {to_utc(slot) for slot in busy_local_starts}


async def create_google_event_for_meeting(
    session: AsyncSession,
    *,
    settings: Settings,
    owner_user_id: int,
    request: BookingRequest,
    meeting: Meeting,
    client: User,
) -> Meeting:
    profile = await get_owner_profile(session, owner_user_id)
    if not _google_calendar_ready(profile):
        return meeting

    access_token = await get_valid_google_access_token(
        session,
        settings=settings,
        owner_user_id=owner_user_id,
    )
    event = await create_google_meeting_event(
        access_token,
        calendar_id=profile.calendar_id,
        title=request.title,
        description=_build_google_event_description(request, client),
        start_at=request.start_at,
        end_at=request.end_at,
        attendee_email=client.email,
        request_id=request.id,
    )
    return await update_meeting_google_data(
        session,
        meeting=meeting,
        google_event_id=event.event_id,
        google_event_url=event.event_url,
        google_meet_url=event.meet_url,
    )


def _google_calendar_ready(profile: OwnerProfile | None) -> bool:
    return bool(profile and profile.google_status == "active" and profile.calendar_id)


async def update_google_event_for_reschedule(
    session: AsyncSession,
    *,
    settings: Settings,
    owner_user_id: int,
    meeting: Meeting,
) -> None:
    profile = await get_owner_profile(session, owner_user_id)
    if not _google_calendar_ready(profile) or not meeting.google_event_id:
        return

    access_token = await get_valid_google_access_token(
        session,
        settings=settings,
        owner_user_id=owner_user_id,
    )
    await update_google_event_time(
        access_token,
        calendar_id=profile.calendar_id,
        event_id=meeting.google_event_id,
        start_at=meeting.start_at,
        end_at=meeting.end_at,
    )


async def cancel_google_event_for_meeting(
    session: AsyncSession,
    *,
    settings: Settings,
    owner_user_id: int,
    meeting: Meeting,
) -> None:
    profile = await get_owner_profile(session, owner_user_id)
    if not _google_calendar_ready(profile) or not meeting.google_event_id:
        return

    access_token = await get_valid_google_access_token(
        session,
        settings=settings,
        owner_user_id=owner_user_id,
    )
    await cancel_google_event(
        access_token,
        calendar_id=profile.calendar_id,
        event_id=meeting.google_event_id,
    )
