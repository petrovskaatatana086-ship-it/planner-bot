"""Refresh Google tokens outside SQLite writer locks, persist with a snapshot check."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import OAuthCredential
from app.db.transactions import run_sqlite_write
from app.domain.intervals import utc
from app.integrations.google_oauth import refresh_access_token
from app.security import decrypt_secret, encrypt_secret


class GoogleTokens:
    def __init__(self, sessions, *, settings, owner_user_id, clock=None):
        self.sessions = sessions
        self.settings = settings
        self.owner_user_id = owner_user_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def __call__(self, *, force_refresh=False):
        async with self.sessions() as session:
            credential = await session.scalar(select(OAuthCredential).where(
                OAuthCredential.user_id == self.owner_user_id, OAuthCredential.provider == "google"))
            if (credential is None or credential.status != "active" or not credential.encrypted_access_token
                    or not self.settings.oauth_encryption_key):
                raise ValueError("GOOGLE_AUTH_REQUIRED")
            access = credential.encrypted_access_token
            refresh = credential.encrypted_refresh_token
            expires = utc(credential.expires_at) if credential.expires_at else None
        key = self.settings.oauth_encryption_key
        if not force_refresh and (expires is None or expires > utc(self.clock())+timedelta(minutes=5)):
            return decrypt_secret(access, key)
        if not refresh:
            raise ValueError("GOOGLE_AUTH_REQUIRED")
        # No session/write transaction remains open during the refresh request.
        refreshed = await refresh_access_token(self.settings, decrypt_secret(refresh, key))
        encrypted = encrypt_secret(refreshed.access_token, key)
        async def persist(session):
            current = await session.scalar(select(OAuthCredential).where(
                OAuthCredential.user_id == self.owner_user_id, OAuthCredential.provider == "google"))
            if current is None or current.status != "active" or current.encrypted_refresh_token != refresh:
                raise ValueError("GOOGLE_AUTH_CHANGED")
            if current.encrypted_access_token != access:
                return decrypt_secret(current.encrypted_access_token, key)
            current.encrypted_access_token = encrypted
            current.expires_at = refreshed.expires_at
            current.last_refresh_at = utc(self.clock())
            return refreshed.access_token
        return await run_sqlite_write(self.sessions, persist)
