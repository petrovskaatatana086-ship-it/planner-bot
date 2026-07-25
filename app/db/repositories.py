from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditLog,
    BookingDraft,
    BookingRequest,
    Meeting,
    OAuthCredential,
    OwnerProfile,
    User,
)
from app.domain.booking import MEETING_DURATION_MINUTES, to_utc
from app.domain.roles import resolve_role


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def ensure_user_stub(
    session: AsyncSession,
    *,
    telegram_id: int,
    owner_telegram_id: int,
    full_name: str,
    username: str | None,
) -> User:
    user = await get_user_by_telegram_id(session, telegram_id)
    role = resolve_role(telegram_id, owner_telegram_id)

    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
            role=role,
            consent_at=datetime.now(timezone.utc) if role == "owner" else None,
        )
        session.add(user)
        await session.flush()
        session.add(
            AuditLog(
                actor_user_id=user.id,
                action="user.created",
                entity_type="user",
                entity_id=str(user.id),
            )
        )
        if role == "owner":
            session.add(
                AuditLog(
                    actor_user_id=user.id,
                    action="owner.assigned",
                    entity_type="user",
                    entity_id=str(user.id),
                )
            )
            await ensure_owner_profile(session, user)
        return user

    user.username = username
    user.full_name = full_name or user.full_name
    user.role = role
    user.last_activity_at = datetime.now(timezone.utc)
    if role == "owner":
        await ensure_owner_profile(session, user)
    return user


async def ensure_owner_profile(session: AsyncSession, owner: User) -> OwnerProfile:
    result = await session.execute(select(OwnerProfile).where(OwnerProfile.user_id == owner.id))
    profile = result.scalar_one_or_none()
    if profile is not None:
        return profile

    profile = OwnerProfile(user_id=owner.id)
    session.add(profile)
    await session.flush()
    session.add(
        AuditLog(
            actor_user_id=owner.id,
            action="owner_profile.created",
            entity_type="owner_profile",
            entity_id=str(profile.id),
        )
    )
    return profile


async def get_owner_profile(session: AsyncSession, owner_user_id: int) -> OwnerProfile | None:
    result = await session.execute(select(OwnerProfile).where(OwnerProfile.user_id == owner_user_id))
    return result.scalar_one_or_none()


async def get_active_owner_profile(session: AsyncSession) -> OwnerProfile | None:
    result = await session.execute(
        select(OwnerProfile).join(User, OwnerProfile.user_id == User.id).where(User.role == "owner")
    )
    return result.scalar_one_or_none()


async def update_owner_profile_field(
    session: AsyncSession,
    *,
    owner: User,
    field: str,
    value: int | bool,
) -> OwnerProfile:
    profile = await ensure_owner_profile(session, owner)
    setattr(profile, field, value)
    session.add(
        AuditLog(
            actor_user_id=owner.id,
            action=f"owner_profile.{field}.updated",
            entity_type="owner_profile",
            entity_id=str(profile.id),
        )
    )
    return profile


async def get_google_oauth_credential(
    session: AsyncSession,
    user_id: int,
) -> OAuthCredential | None:
    result = await session.execute(
        select(OAuthCredential).where(
            OAuthCredential.user_id == user_id,
            OAuthCredential.provider == "google",
        )
    )
    return result.scalar_one_or_none()


async def mark_google_oauth_pending(
    session: AsyncSession,
    owner: User,
    *,
    oauth_state: str | None = None,
) -> OAuthCredential:
    credential = await get_google_oauth_credential(session, owner.id)
    if credential is None:
        credential = OAuthCredential(
            user_id=owner.id,
            provider="google",
            status="pending",
            oauth_state=oauth_state,
        )
        session.add(credential)
        await session.flush()
    else:
        credential.status = "pending"
        credential.last_error = None
        credential.oauth_state = oauth_state

    session.add(
        AuditLog(
            actor_user_id=owner.id,
            action="google_oauth.pending",
            entity_type="oauth_credential",
            entity_id=str(credential.id),
        )
    )
    return credential


async def complete_google_oauth(
    session: AsyncSession,
    *,
    owner_user_id: int,
    oauth_state: str,
    encrypted_access_token: str,
    encrypted_refresh_token: str | None,
    expires_at: datetime | None,
) -> OAuthCredential:
    credential = await get_google_oauth_credential(session, owner_user_id)
    if credential is None:
        raise ValueError("OAuth credential was not started")
    if credential.oauth_state != oauth_state:
        raise ValueError("OAuth state does not match")

    credential.encrypted_access_token = encrypted_access_token
    if encrypted_refresh_token is not None:
        credential.encrypted_refresh_token = encrypted_refresh_token
    credential.expires_at = expires_at
    credential.status = "active"
    credential.oauth_state = None
    credential.last_error = None

    profile = await get_owner_profile(session, owner_user_id)
    if profile is not None:
        profile.google_status = "active"

    session.add(
        AuditLog(
            actor_user_id=owner_user_id,
            action="google_oauth.connected",
            entity_type="oauth_credential",
            entity_id=str(credential.id),
        )
    )
    return credential


async def update_google_access_token(
    session: AsyncSession,
    *,
    owner_user_id: int,
    encrypted_access_token: str,
    expires_at: datetime | None,
) -> OAuthCredential:
    credential = await get_google_oauth_credential(session, owner_user_id)
    if credential is None:
        raise ValueError("OAuth credential not found")

    credential.encrypted_access_token = encrypted_access_token
    credential.expires_at = expires_at
    credential.status = "active"
    credential.last_refresh_at = datetime.now(timezone.utc)
    credential.last_error = None
    session.add(
        AuditLog(
            actor_user_id=owner_user_id,
            action="google_oauth.access_token_refreshed",
            entity_type="oauth_credential",
            entity_id=str(credential.id),
        )
    )
    return credential


async def set_owner_google_calendar(
    session: AsyncSession,
    *,
    owner_user_id: int,
    calendar_id: str,
) -> OwnerProfile:
    profile = await get_owner_profile(session, owner_user_id)
    if profile is None:
        raise ValueError("Owner profile not found")

    profile.calendar_id = calendar_id
    session.add(
        AuditLog(
            actor_user_id=owner_user_id,
            action="owner_profile.calendar_id.updated",
            entity_type="owner_profile",
            entity_id=str(profile.id),
        )
    )
    return profile


async def fail_google_oauth(
    session: AsyncSession,
    *,
    owner_user_id: int,
    error: str,
) -> None:
    credential = await get_google_oauth_credential(session, owner_user_id)
    if credential is not None:
        credential.status = "error"
        credential.last_error = error
    profile = await get_owner_profile(session, owner_user_id)
    if profile is not None:
        profile.google_status = "error"
    session.add(
        AuditLog(
            actor_user_id=owner_user_id,
            action="google_oauth.failed",
            entity_type="oauth_credential",
            entity_id=str(credential.id) if credential is not None else None,
            metadata_json=error,
        )
    )


async def complete_client_registration(
    session: AsyncSession,
    *,
    user: User,
    full_name: str,
    email: str,
) -> User:
    user.full_name = full_name.strip()
    user.email = email.strip()
    user.consent_at = datetime.now(timezone.utc)
    user.status = "active"
    session.add(
        AuditLog(
            actor_user_id=user.id,
            action="client.registered",
            entity_type="user",
            entity_id=str(user.id),
        )
    )
    return user


async def create_booking_draft(session: AsyncSession, client_id: int) -> BookingDraft:
    draft = BookingDraft(
        client_id=client_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    session.add(draft)
    await session.flush()
    session.add(
        AuditLog(
            actor_user_id=client_id,
            action="booking_draft.created",
            entity_type="booking_draft",
            entity_id=str(draft.id),
        )
    )
    return draft


async def get_busy_starts(session: AsyncSession) -> set[datetime]:
    request_result = await session.execute(
        select(BookingRequest.start_at).where(BookingRequest.status.in_({"pending", "approved"}))
    )
    meeting_result = await session.execute(
        select(Meeting.start_at).where(
            Meeting.status.in_({"confirmed", "rescheduled", "reschedule_pending", "rescheduled_pending"})
        )
    )
    values = list(request_result.scalars().all()) + list(meeting_result.scalars().all())
    return {
        value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        for value in values
    }


async def create_booking_request_from_draft(
    session: AsyncSession,
    *,
    client_id: int,
    selected_start_local: datetime,
    title: str,
    comment: str | None,
    duration_minutes: int = MEETING_DURATION_MINUTES,
) -> BookingRequest:
    start_at = to_utc(selected_start_local)
    request = BookingRequest(
        client_id=client_id,
        title=title,
        comment=comment,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=duration_minutes),
        status="pending",
        reserved_until=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    session.add(request)
    await session.flush()
    session.add(
        AuditLog(
            actor_user_id=client_id,
            action="booking_request.created",
            entity_type="booking_request",
            entity_id=str(request.id),
        )
    )
    return request


async def list_client_requests(session: AsyncSession, client_id: int) -> list[BookingRequest]:
    result = await session.execute(
        select(BookingRequest)
        .where(BookingRequest.client_id == client_id)
        .order_by(BookingRequest.created_at.desc())
        .limit(10)
    )
    return list(result.scalars().all())


async def list_client_meetings(session: AsyncSession, client_id: int) -> list[Meeting]:
    result = await session.execute(
        select(Meeting)
        .where(Meeting.client_id == client_id)
        .order_by(Meeting.start_at.asc())
        .limit(10)
    )
    return list(result.scalars().all())


async def get_meeting(session: AsyncSession, meeting_id: int) -> Meeting | None:
    result = await session.execute(select(Meeting).where(Meeting.id == meeting_id))
    return result.scalar_one_or_none()


async def list_pending_requests(session: AsyncSession) -> list[tuple[BookingRequest, User]]:
    result = await session.execute(
        select(BookingRequest, User)
        .join(User, BookingRequest.client_id == User.id)
        .where(BookingRequest.status == "pending")
        .order_by(BookingRequest.created_at.asc())
    )
    return list(result.all())


async def get_booking_request(session: AsyncSession, request_id: int) -> BookingRequest | None:
    result = await session.execute(select(BookingRequest).where(BookingRequest.id == request_id))
    return result.scalar_one_or_none()


async def approve_booking_request(
    session: AsyncSession,
    *,
    request: BookingRequest,
    actor_user_id: int,
) -> Meeting:
    request.status = "approved"
    request.approved_at = datetime.now(timezone.utc)
    meeting = Meeting(
        booking_request_id=request.id,
        client_id=request.client_id,
        title=request.title,
        start_at=request.start_at,
        end_at=request.end_at,
        status="confirmed",
    )
    session.add(meeting)
    await session.flush()
    session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action="booking_request.approved",
            entity_type="booking_request",
            entity_id=str(request.id),
        )
    )
    return meeting


async def update_meeting_google_data(
    session: AsyncSession,
    *,
    meeting: Meeting,
    google_event_id: str,
    google_meet_url: str | None,
) -> Meeting:
    meeting.google_event_id = google_event_id
    meeting.google_meet_url = google_meet_url
    session.add(
        AuditLog(
            actor_user_id=meeting.client_id,
            action="meeting.google_event_created",
            entity_type="meeting",
            entity_id=str(meeting.id),
        )
    )
    return meeting


async def reschedule_meeting(
    session: AsyncSession,
    *,
    meeting: Meeting,
    start_at: datetime,
    end_at: datetime,
    actor_user_id: int,
) -> Meeting:
    meeting.start_at = start_at
    meeting.end_at = end_at
    meeting.status = "rescheduled"
    meeting.proposed_start_at = None
    meeting.proposed_end_at = None
    meeting.reschedule_comment = None
    session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action="meeting.rescheduled",
            entity_type="meeting",
            entity_id=str(meeting.id),
        )
    )
    return meeting


async def request_meeting_reschedule(
    session: AsyncSession,
    *,
    meeting: Meeting,
    proposed_start_at: datetime,
    proposed_end_at: datetime,
    comment: str | None,
    actor_user_id: int,
) -> Meeting:
    meeting.status = "reschedule_pending"
    meeting.proposed_start_at = proposed_start_at
    meeting.proposed_end_at = proposed_end_at
    meeting.reschedule_comment = comment
    session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action="meeting.reschedule_requested",
            entity_type="meeting",
            entity_id=str(meeting.id),
            metadata_json=comment,
        )
    )
    return meeting


async def approve_meeting_reschedule(
    session: AsyncSession,
    *,
    meeting: Meeting,
    actor_user_id: int,
) -> Meeting:
    if meeting.proposed_start_at is None or meeting.proposed_end_at is None:
        raise ValueError("Meeting does not have proposed time")
    return await reschedule_meeting(
        session,
        meeting=meeting,
        start_at=meeting.proposed_start_at,
        end_at=meeting.proposed_end_at,
        actor_user_id=actor_user_id,
    )


async def reject_meeting_reschedule(
    session: AsyncSession,
    *,
    meeting: Meeting,
    actor_user_id: int,
    reason: str,
) -> Meeting:
    meeting.status = "confirmed"
    meeting.proposed_start_at = None
    meeting.proposed_end_at = None
    meeting.reschedule_comment = None
    session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action="meeting.reschedule_rejected",
            entity_type="meeting",
            entity_id=str(meeting.id),
            metadata_json=reason.strip(),
        )
    )
    return meeting


async def cancel_meeting(
    session: AsyncSession,
    *,
    meeting: Meeting,
    actor_user_id: int,
    reason: str,
    status: str = "cancelled_by_owner",
) -> Meeting:
    meeting.status = status
    meeting.proposed_start_at = None
    meeting.proposed_end_at = None
    meeting.reschedule_comment = None
    session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=f"meeting.{status}",
            entity_type="meeting",
            entity_id=str(meeting.id),
            metadata_json=reason.strip(),
        )
    )
    return meeting


async def reject_booking_request(
    session: AsyncSession,
    *,
    request: BookingRequest,
    actor_user_id: int,
    reason: str,
) -> BookingRequest:
    request.status = "rejected"
    request.rejection_reason = reason.strip()
    request.rejected_at = datetime.now(timezone.utc)
    session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action="booking_request.rejected",
            entity_type="booking_request",
            entity_id=str(request.id),
        )
    )
    return request
