"""Shared registration and owner settings, independent of Telegram/HTTP adapters."""
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import AuditLog, OperationResult, OwnerProfile, ScheduleRule, User
from app.domain.booking import WORKDAYS, WORK_START, WORK_END
from app.db.repositories import ensure_user_stub
from app.db.transactions import run_sqlite_write
from app.domain.email import is_valid_email
from app.domain.intervals import utc
from app.domain.registration import CONSENT_VERSION, valid_name
from app.services.booking import BookingError, check_version

SETTING_RANGES = {
    "default_duration": (15, 240), "buffer_minutes": (0, 120), "daily_limit": (1, 50),
    "booking_horizon_days": (1, 180), "min_booking_notice_hours": (1, 720),
}
PROFILE_FIELDS = (*SETTING_RANGES, "booking_enabled")
SETTING_FIELDS = (*PROFILE_FIELDS, "working_days")


def settings_data(profile, rules=()):
    days = sorted({r.weekday for r in rules if r.is_active}) if rules else sorted(WORKDAYS)
    return {**{field: getattr(profile, field) for field in PROFILE_FIELDS}, "working_days": days,
            "version": profile.version, "timezone": "Europe/Moscow",
            "google_status": profile.google_status, "google_email": profile.google_email}


def validate_changes(changes):
    if not isinstance(changes, dict) or not changes or set(changes)-set(SETTING_FIELDS):
        raise BookingError("VALIDATION_FAILED")
    for field, value in changes.items():
        if field == "working_days":
            if (not isinstance(value, list) or len(value) > 7
                    or any(type(day) is not int or not 0 <= day <= 6 for day in value)
                    or len(set(value)) != len(value)):
                raise BookingError("VALIDATION_FAILED")
        elif field == "booking_enabled":
            if type(value) is not bool:
                raise BookingError("VALIDATION_FAILED")
        elif type(value) is not int or not SETTING_RANGES[field][0] <= value <= SETTING_RANGES[field][1]:
            raise BookingError("VALIDATION_FAILED")


class ProfileCommands:
    def __init__(self, sessions, *, owner_telegram_id, consent_version=CONSENT_VERSION, clock=None):
        if not isinstance(consent_version, str) or not 1 <= len(consent_version) <= 64:
            raise ValueError("A published consent version is required")
        self.sessions = sessions
        self.owner_telegram_id = owner_telegram_id
        self.consent_version = consent_version
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def ensure_telegram_user(self, *, telegram_id, full_name, username=None):
        """Identity must come from Telegram or verified initData, never a JSON actor field."""
        if type(telegram_id) is not int or telegram_id <= 0 or not valid_name(full_name):
            raise BookingError("INVALID_AUTH")
        if username is not None and (not isinstance(username, str) or len(username) > 255):
            raise BookingError("INVALID_AUTH")
        async def ensure(session):
            user = await ensure_user_stub(session, telegram_id=telegram_id,
                owner_telegram_id=self.owner_telegram_id, full_name=full_name.strip(), username=username)
            await session.flush()
            return user
        return await run_sqlite_write(self.sessions, ensure)

    async def _run(self, actor_id, command_id, kind, payload, operation, *, owner=False):
        if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
            raise BookingError("INVALID_REQUEST")
        digest = hashlib.sha256(json.dumps({"kind": kind, "payload": payload},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        async def execute(session):
            actor = await session.get(User, actor_id)
            if actor is None or actor.status != "active":
                raise BookingError("ACCOUNT_BLOCKED")
            is_owner = actor.telegram_id == self.owner_telegram_id
            if (owner and not is_owner) or (not owner and is_owner):
                raise BookingError("FORBIDDEN")
            prior = await session.scalar(select(OperationResult).where(
                OperationResult.actor_id == actor_id, OperationResult.command_id == command_id))
            if prior:
                if prior.payload_hash != digest:
                    raise BookingError("IDEMPOTENCY_KEY_REUSED")
                return json.loads(prior.response_json)
            now = utc(self.clock())
            result, identifier, entity_type = await operation(session, actor, now)
            session.add(OperationResult(actor_id=actor_id, command_id=command_id, kind=kind,
                payload_hash=digest, resource_id=identifier, response_json=json.dumps(result, sort_keys=True), created_at=now))
            session.add(AuditLog(actor_user_id=actor_id, action=kind, entity_type=entity_type,
                                 entity_id=str(identifier), created_at=now))
            return result
        return await run_sqlite_write(self.sessions, execute)

    async def register(self, *, actor_id, command_id, full_name, email, consent, consent_version):
        if not valid_name(full_name) or not isinstance(email, str) or not is_valid_email(email) or consent is not True:
            raise BookingError("VALIDATION_FAILED")
        full_name, email = full_name.strip(), email.strip()
        payload = dict(full_name=full_name, email=email, consent=True, consent_version=consent_version)
        async def complete(session, actor, now):
            if actor.email and actor.consent_at:
                raise BookingError("INVALID_STATE")
            if consent_version != self.consent_version:
                raise BookingError("CONSENT_CHANGED")
            actor.full_name, actor.email = full_name, email
            actor.consent_at, actor.consent_version = now, consent_version
            return dict(id=actor.id, full_name=full_name, email=email,
                consent_version=consent_version, registration_required=False), actor.id, "user"
        return await self._run(actor_id, command_id, "registration.completed", payload, complete)

    async def patch_settings(self, *, actor_id, command_id, changes, expected_version):
        validate_changes(changes)
        if type(expected_version) is not int or expected_version < 1:
            raise BookingError("VERSION_REQUIRED")
        return await self._settings(actor_id, command_id, dict(changes), expected_version)

    async def set_client_access(self, *, actor_id, command_id, client_id, blocked):
        """Absolute access command; replay never reapplies an older access decision."""
        if type(client_id) is not int or client_id <= 0 or type(blocked) is not bool:
            raise BookingError("VALIDATION_FAILED")
        status = "blocked" if blocked else "active"
        async def update(session, actor, now):
            client = await session.get(User, client_id)
            if client is None or client.telegram_id == self.owner_telegram_id:
                raise BookingError("RESOURCE_NOT_FOUND")
            if client.status not in {"active", "blocked"}:
                raise BookingError("INVALID_STATE")
            changed = client.status != status
            client.status = status
            return dict(id=client.id, status=status, changed=changed), client.id, "user"
        return await self._run(actor_id, command_id, f"client.status.{status}",
            dict(client_id=client_id, blocked=blocked), update, owner=True)

    async def apply_settings_message(self, *, actor_id, command_id, changes):
        """Absolute owner text command: apply to latest state inside the writer lock.

        HTTP card/PATCH must use patch_settings with an explicit version. Telegram
        message identity supplies a durable key; re-delivery must not rebind it to
        a freshly read expected_version and change its hash after success.
        """
        validate_changes(changes)
        return await self._settings(actor_id, command_id, dict(changes), None)

    async def _settings(self, actor_id, command_id, changes, expected_version):
        payload = dict(changes=changes, expected_version=expected_version)
        async def update(session, actor, now):
            profile = await session.scalar(select(OwnerProfile).where(OwnerProfile.user_id == actor.id))
            if profile is None:
                raise BookingError("BOOKING_UNAVAILABLE")
            if expected_version is not None:
                check_version(profile, expected_version)
            rules = list((await session.scalars(select(ScheduleRule).where(
                ScheduleRule.owner_id == actor.id))).all())
            before = settings_data(profile, rules)
            changed = any(before[field] != (sorted(value) if field == "working_days" else value)
                          for field, value in changes.items())
            for field, value in changes.items():
                if field == "working_days":
                    # Materialize all days, including inactive ones: no rules means
                    # the historical Mon–Fri fallback, not an entirely closed week.
                    for day in range(7):
                        existing = [rule for rule in rules if rule.weekday == day]
                        if not existing:
                            rule = ScheduleRule(owner_id=actor.id, weekday=day,
                                start_time=WORK_START, end_time=WORK_END, is_active=day in value)
                            session.add(rule)
                            rules.append(rule)
                        else:
                            for rule in existing:
                                rule.is_active = day in value
                else:
                    setattr(profile, field, value)
            if changed:
                profile.version += 1
            return settings_data(profile, rules), profile.id, "owner_profile"
        return await self._run(actor_id, command_id, "settings.updated", payload, update, owner=True)
