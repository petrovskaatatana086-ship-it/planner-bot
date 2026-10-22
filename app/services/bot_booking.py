"""Trusted Telegram adapter: bind a message to versions once, then call commands.

Only call after verifying Telegram actor identity. No network in input capture.
Captured inputs deliberately survive failed operations: a changed resource needs
a new message. They must be retained with OperationResult during future cleanup.
"""
import hashlib
import json
from datetime import datetime

from sqlalchemy import select

from app.db.models import BotCommandInput, BookingRequest, Meeting, OwnerProfile, RescheduleRequest, User
from app.db.transactions import run_sqlite_write
from app.services.booking import BookingError
from app.services.outbox import DeliveryError


class BotBooking:
    OWNER_ACTIONS = {"approve_request", "reject_request", "reschedule_by_owner",
                     "approve_reschedule", "reject_reschedule"}
    ACTIONS = OWNER_ACTIONS | {"create_request", "request_reschedule", "cancel_meeting"}

    def __init__(self, commands):
        self.commands = commands
        self.sessions = commands.sessions

    async def execute(self, action, *, actor_id, command_id, **inputs):
        if action not in self.ACTIONS or not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
            raise BookingError("INVALID_REQUEST")
        # Date/time is canonicalized before hashing and storing, not reconstructed
        # from the current resource on replay.
        inputs = dict(inputs)
        if "start_at" in inputs:
            value = inputs["start_at"]
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise BookingError("VALIDATION_FAILED")
            inputs["start_at"] = value.isoformat()
        serialized = json.dumps(dict(action=action, inputs=inputs), sort_keys=True,
                                separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(serialized.encode()).hexdigest()

        async def capture(session):
            actor = await session.get(User, actor_id)
            if actor is None or actor.status != "active":
                raise BookingError("ACCOUNT_BLOCKED")
            owner = actor.telegram_id == self.commands.owner_telegram_id
            if action in self.OWNER_ACTIONS and not owner:
                raise BookingError("FORBIDDEN")
            if not owner and (not actor.email or not actor.consent_at):
                raise BookingError("REGISTRATION_REQUIRED")
            prior = await session.scalar(select(BotCommandInput).where(
                BotCommandInput.actor_id == actor_id, BotCommandInput.command_id == command_id))
            if prior:
                if prior.input_hash != digest:
                    raise BookingError("IDEMPOTENCY_KEY_REUSED")
                return json.loads(prior.payload_json)
            payload = dict(inputs)
            profile = await session.scalar(select(OwnerProfile).join(User).where(
                User.telegram_id == self.commands.owner_telegram_id))
            if profile is None:
                raise BookingError("OWNER_UNAVAILABLE")
            if action in {"approve_request", "reject_request"}:
                resource = await session.get(BookingRequest, payload.get("request_id"))
            elif action != "create_request":
                resource = await session.get(Meeting, payload.get("meeting_id"))
                if resource is not None and not owner and resource.client_id != actor_id:
                    raise BookingError("RESOURCE_NOT_FOUND")
            else:
                resource = None
            if action != "create_request":
                if resource is None:
                    raise BookingError("RESOURCE_NOT_FOUND")
                payload.setdefault("expected_version", resource.version)
            if action in {"request_reschedule", "reschedule_by_owner"}:
                payload["settings_version"] = profile.version
            if action in {"approve_reschedule", "reject_reschedule"}:
                proposal_id = await session.scalar(select(RescheduleRequest.id).where(
                    RescheduleRequest.meeting_id == resource.id, RescheduleRequest.status == "pending"))
                if proposal_id is None:
                    raise BookingError("INVALID_STATE")
                payload.setdefault("reschedule_request_id", proposal_id)
            session.add(BotCommandInput(actor_id=actor_id, command_id=command_id,
                input_hash=digest, payload_json=json.dumps(payload, sort_keys=True, ensure_ascii=False)))
            return payload

        payload = await run_sqlite_write(self.sessions, capture)
        if "start_at" in payload:
            payload["start_at"] = datetime.fromisoformat(payload["start_at"])
        try:
            return await getattr(self.commands, action)(actor_id=actor_id, command_id=command_id, **payload)
        except DeliveryError as error:
            # Binding/sync enqueue errors roll back with the booking mutation.
            # Expose only their safe code through the same bot error contract.
            raise BookingError(error.code) from None
