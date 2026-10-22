"""Google binding writes. Network requests must happen between prepare and complete.

Existing remote work pins the binding: migration of events is a separate operation.
OAuth secrets live only in OAuthCredential, never OperationResult or AuditLog.
"""
import secrets

from sqlalchemy import or_, select

from app.db.models import AuditLog, CalendarSync, Meeting, OAuthCredential, OutboxEvent, OwnerProfile, User
from app.db.transactions import run_sqlite_write
from app.domain.email import is_valid_email
from app.services.booking import BookingError, check_version
from app.services.profiles import ProfileCommands


class GoogleBindingCommands(ProfileCommands):
    async def selection_version(self, *, actor_id):
        async with self.sessions() as session:
            profile = await self._owner_profile(session, actor_id)
            credential = await self._credential(session, actor_id)
            if credential is None or credential.status != "active" or profile.google_status != "active":
                raise BookingError("CALENDAR_UNAVAILABLE")
            return profile.version

    async def _owner_profile(self, session, actor_id):
        actor = await session.get(User, actor_id)
        if actor is None or actor.status != "active":
            raise BookingError("ACCOUNT_BLOCKED")
        if actor.telegram_id != self.owner_telegram_id:
            raise BookingError("FORBIDDEN")
        profile = await session.scalar(select(OwnerProfile).where(OwnerProfile.user_id == actor.id))
        if profile is None:
            raise BookingError("BOOKING_UNAVAILABLE")
        return profile

    async def _credential(self, session, actor_id):
        return await session.scalar(select(OAuthCredential).where(
            OAuthCredential.user_id == actor_id, OAuthCredential.provider == "google"))

    async def _pinned(self, session):
        # One owner per database. Even historical syncs retain their calendar identity.
        return (await session.scalar(select(CalendarSync.meeting_id).limit(1)) is not None
                or await session.scalar(select(OutboxEvent.id).where(OutboxEvent.channel == "google").limit(1)) is not None
                or await session.scalar(select(Meeting.id).where(or_(
                    Meeting.google_event_id.is_not(None),
                    ~Meeting.status.in_(("cancelled_by_owner", "cancelled_by_client")))).limit(1)) is not None)

    async def select_calendar(self, *, actor_id, command_id, calendar_id, expected_version):
        if (not isinstance(calendar_id, str) or not 1 <= len(calendar_id) <= 512
                or calendar_id != calendar_id.strip() or any(ord(c) < 32 for c in calendar_id)):
            raise BookingError("VALIDATION_FAILED")
        async def update(session, actor, now):
            profile = await self._owner_profile(session, actor.id)
            check_version(profile, expected_version)
            credential = await self._credential(session, actor.id)
            if credential is None or credential.status != "active" or profile.google_status != "active":
                raise BookingError("CALENDAR_UNAVAILABLE")
            if profile.calendar_id != calendar_id:
                if await self._pinned(session):
                    raise BookingError("CALENDAR_BINDING_IN_USE")
                profile.calendar_id = calendar_id
                profile.version += 1
            return dict(calendar_id=profile.calendar_id, version=profile.version), profile.id, "owner_profile"
        return await self._run(actor_id, command_id, "google.calendar.selected",
            dict(calendar_id=calendar_id, expected_version=expected_version), update, owner=True)

    async def begin_oauth(self, *, actor_id, oauth_state):
        if not isinstance(oauth_state, str) or not 1 <= len(oauth_state) <= 255:
            raise BookingError("INVALID_REQUEST")
        async def begin(session):
            await self._owner_profile(session, actor_id)
            credential = await self._credential(session, actor_id)
            if credential is None:
                credential = OAuthCredential(user_id=actor_id, provider="google", status="pending")
                session.add(credential)
            # Reconnect must not disable usable tokens while the browser is open.
            if credential.status != "active":
                credential.status = "pending"
            credential.oauth_state = oauth_state
            credential.last_error = None
            session.add(AuditLog(actor_user_id=actor_id, action="google_oauth.pending",
                                 entity_type="owner_profile"))
        await run_sqlite_write(self.sessions, begin)

    async def _pending(self, session, actor_id, state):
        profile = await self._owner_profile(session, actor_id)
        credential = await self._credential(session, actor_id)
        if (not isinstance(state, str) or not state or credential is None or not credential.oauth_state
                or not secrets.compare_digest(credential.oauth_state.encode(), state.encode())):
            raise BookingError("INVALID_OAUTH_STATE")
        return profile, credential

    async def prepare_oauth(self, *, actor_id, oauth_state):
        # Closed before exchange_code_for_tokens/userinfo; no writer lock during network.
        async with self.sessions() as session:
            profile, _ = await self._pending(session, actor_id, oauth_state)
            return profile.version

    async def complete_oauth(self, *, actor_id, oauth_state, expected_version,
                             encrypted_access_token, encrypted_refresh_token, expires_at, google_email):
        if not isinstance(google_email, str) or not is_valid_email(google_email):
            raise BookingError("VALIDATION_FAILED")
        email = google_email.strip().lower()
        async def complete(session):
            profile, credential = await self._pending(session, actor_id, oauth_state)
            check_version(profile, expected_version)
            same_account = bool(profile.google_email and profile.google_email.lower() == email)
            if not same_account and await self._pinned(session):
                raise BookingError("CALENDAR_BINDING_IN_USE")
            refresh = encrypted_refresh_token or (credential.encrypted_refresh_token if same_account else None)
            if not encrypted_access_token or not refresh:
                raise BookingError("GOOGLE_REFRESH_REQUIRED")
            if not same_account:
                profile.calendar_id = None
            credential.encrypted_access_token = encrypted_access_token
            credential.encrypted_refresh_token = refresh
            credential.expires_at = expires_at
            credential.status = "active"
            credential.oauth_state = None
            credential.last_error = None
            profile.google_status = "active"
            profile.google_email = email
            profile.version += 1
            session.add(AuditLog(actor_user_id=actor_id, action="google_oauth.connected",
                                 entity_type="owner_profile", entity_id=str(profile.id)))
        await run_sqlite_write(self.sessions, complete)

    async def fail_oauth(self, *, actor_id, oauth_state):
        async def fail(session):
            try:
                profile, credential = await self._pending(session, actor_id, oauth_state)
            except BookingError:
                return False
            credential.oauth_state = None
            credential.last_error = "GOOGLE_OAUTH_FAILED"
            if credential.status != "active":
                credential.status = "error"
                if profile.google_status != "error":
                    profile.google_status = "error"
                    profile.version += 1
            session.add(AuditLog(actor_user_id=actor_id, action="google_oauth.failed",
                                 entity_type="owner_profile", entity_id=str(profile.id)))
            return True
        return await run_sqlite_write(self.sessions, fail)
