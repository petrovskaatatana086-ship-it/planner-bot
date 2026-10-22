"""Telegram HMAC validation and revocable, non-sliding server sessions."""
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from sqlalchemy import delete

from app.db.models import ApiSession, User
from app.db.transactions import run_sqlite_write
from app.domain.intervals import utc
from app.domain.registration import valid_name
from app.services.booking import BookingError
from app.services.profiles import ProfileCommands


def validate_init_data(raw, bot_token, now):
    try:
        if not isinstance(raw, str) or len(raw.encode()) > 16384 or re.search(r"%(?![0-9a-fA-F]{2})", raw):
            raise ValueError
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, encoding="utf-8", errors="strict", max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs) or not {"hash", "auth_date", "user"} <= fields.keys():
            raise ValueError
        received = fields.pop("hash")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", received):
            raise ValueError
        secret = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received.lower()):
            raise ValueError
        if not re.fullmatch(r"[0-9]{1,12}", fields["auth_date"]):
            raise ValueError
        age = utc(now).timestamp() - int(fields["auth_date"])
        if age > 300 or age < -30:
            raise ValueError
        user = json.loads(fields["user"])
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
            raise ValueError
        if not valid_name(user.get("first_name")):
            raise ValueError
        last = user.get("last_name", "")
        if not isinstance(last, str):
            raise ValueError
        name = (user["first_name"] + " " + last).strip()
        if not valid_name(name):
            raise ValueError
        username = user.get("username")
        if username is not None and (not isinstance(username, str) or len(username) > 255):
            raise ValueError
        return dict(telegram_id=user["id"], full_name=name, username=username)
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise BookingError("INVALID_AUTH") from None


class MiniAppAuth:
    def __init__(self, sessions, settings, *, clock=None):
        self.sessions, self.settings = sessions, settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.profiles = ProfileCommands(sessions, owner_telegram_id=settings.owner_telegram_id, clock=self.clock)

    async def exchange(self, raw):
        identity = validate_init_data(raw, self.settings.bot_token, self.clock())
        actor = await self.profiles.ensure_telegram_user(**identity)
        token = secrets.token_urlsafe(32)
        expires = utc(self.clock()) + timedelta(hours=1)
        async def save(session):
            current = await session.get(User, actor.id)
            if current is None or current.status != "active":
                raise BookingError("ACCOUNT_BLOCKED")
            await session.execute(delete(ApiSession).where(ApiSession.expires_at <= self.clock()))
            session.add(ApiSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                user_id=actor.id, expires_at=expires))
        await run_sqlite_write(self.sessions, save)
        return dict(token=token, expires_at=expires.isoformat())

    async def resolve(self, token):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise BookingError("INVALID_AUTH")
        async with self.sessions() as session:
            row = await session.get(ApiSession, hashlib.sha256(token.encode()).hexdigest())
            if row is None or row.revoked_at or utc(row.expires_at) <= utc(self.clock()):
                raise BookingError("SESSION_EXPIRED")
            actor = await session.get(User, row.user_id)
            if actor is None or actor.status != "active":
                raise BookingError("ACCOUNT_BLOCKED")
            return actor

    async def revoke(self, token):
        async def remove(session):
            row = await session.get(ApiSession, hashlib.sha256(token.encode()).hexdigest())
            if row is not None and row.revoked_at is None:
                row.revoked_at = utc(self.clock())
        await run_sqlite_write(self.sessions, remove)
