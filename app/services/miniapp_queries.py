"""Allowlisted DTOs with role/ownership checks inside a consistent read snapshot."""
from datetime import datetime, time, timedelta

from sqlalchemy import func, select, text, literal, or_, and_

from app.db.models import BookingRequest, CalendarSync, Meeting, OwnerProfile, RescheduleRequest, User
from app.domain.booking import MOSCOW_TZ
from app.domain.intervals import utc
from app.services.booking import ACTIVE, BookingError
from app.services.profiles import settings_data


def stamp(value):
    return utc(value).isoformat().replace("+00:00", "Z") if value else None


def profile_data(actor, owner):
    return dict(role="owner" if owner else "client", status=actor.status,
        registration_required=not owner and not (actor.email and actor.consent_at),
        profile=dict(id=actor.id, full_name=actor.full_name, email=actor.email), timezone="Europe/Moscow")


class MiniAppQueries:
    def __init__(self, sessions, settings, clock):
        self.sessions, self.settings, self.clock = sessions, settings, clock

    async def _meeting(self, session, row, owner):
        if row is None:
            return None
        now = utc(self.clock())
        sync = await session.get(CalendarSync, row.id)
        proposals = (await session.scalars(select(RescheduleRequest).where(
            RescheduleRequest.meeting_id == row.id).order_by(RescheduleRequest.id.desc()).limit(2))).all()
        pending = next((p for p in proposals if p.status == "pending"), None)
        last = next((p for p in proposals if p.status != "pending"), None)
        active = row.status in ACTIVE and utc(row.start_at) > now
        actions = []
        if active:
            actions.append("cancel")
            if owner or row.status in ("confirmed", "rescheduled"):
                actions.append("reschedule")
            if owner and pending:
                actions.extend(("approve_reschedule", "reject_reschedule"))
        data = {key: getattr(row, key) for key in (
            "id", "version", "booking_request_id", "title", "status", "cancellation_reason")}
        # Only HTTPS provider links; raw legacy URLs must not become executable UI links.
        for key in ("google_meet_url", "google_event_url"):
            value = getattr(row, key)
            data[key] = value if value and value.startswith("https://") else None
        data.update(start_at=stamp(row.start_at), end_at=stamp(row.end_at), timezone="Europe/Moscow",
            is_past=utc(row.end_at) <= now, integration_state=sync.state if sync else "not_connected",
            pending_reschedule=self.proposal(pending), last_reschedule_decision=self.proposal(last), allowed_actions=actions)
        if owner:
            data["client"] = await self.client(session, row.client_id)
        return data

    @staticmethod
    def proposal(row):
        if row is None:
            return None
        return {**{k: getattr(row, k) for k in ("id", "meeting_id", "comment", "status", "reason")},
            **{k: stamp(getattr(row, k)) for k in ("proposed_start_at", "proposed_end_at", "created_at", "decided_at")}}

    @staticmethod
    async def client(session, identifier):
        row = await session.get(User, identifier)
        return dict(id=row.id, full_name=row.full_name, email=row.email) if row else None

    async def _request(self, session, row, owner):
        past = utc(row.start_at) <= utc(self.clock())
        data = {k: getattr(row, k) for k in ("id", "version", "title", "comment", "status", "rejection_reason")}
        data.update({k: stamp(getattr(row, k)) for k in ("start_at", "end_at", "reserved_until", "created_at")})
        meeting = await session.scalar(select(Meeting).where(Meeting.booking_request_id == row.id))
        data.update(timezone="Europe/Moscow", meeting_id=meeting.id if meeting else None,
            meeting=await self._meeting(session, meeting, owner), reservation_active=bool(row.status == "pending"
                and row.reserved_until and utc(row.reserved_until) > utc(self.clock())),
            approval_unavailable_reason="REQUEST_IN_PAST" if row.status == "pending" and past else None,
            allowed_actions=(["reject"] if past else ["approve", "reject"]) if owner and row.status == "pending" else [])
        if owner:
            data["client"] = await self.client(session, row.client_id)
        return data

    async def read(self, actor_id, action, *, identifier=None, first=None, last=None, status="all", view="upcoming", kind="all", limit=20, offset=0):
        async with self.sessions() as session:
            await session.execute(text("BEGIN"))
            actor = await session.get(User, actor_id)
            if actor is None or actor.status != "active":
                raise BookingError("ACCOUNT_BLOCKED")
            owner = actor.telegram_id == self.settings.owner_telegram_id
            if action == "me":
                return profile_data(actor, owner)
            if action.startswith("admin/") and not owner:
                raise BookingError("FORBIDDEN")
            if not owner and (not actor.email or not actor.consent_at):
                raise BookingError("REGISTRATION_REQUIRED")
            if action in ("booking-rules", "admin/settings"):
                profile = await session.scalar(select(OwnerProfile).join(User).where(User.telegram_id == self.settings.owner_telegram_id))
                if profile is None:
                    raise BookingError("BOOKING_UNAVAILABLE")
                result = settings_data(profile)
                if not owner:
                    result.pop("google_email", None)
                    result.pop("google_status", None)
                return result
            if action in ("requests/detail", "admin/requests/detail", "meetings/detail", "admin/meetings/detail"):
                is_meeting = "meetings" in action
                row = await session.get(Meeting if is_meeting else BookingRequest, identifier)
                if row is None or (not owner and row.client_id != actor.id):
                    raise BookingError("NOT_FOUND")
                return await (self._meeting(session, row, owner) if is_meeting else self._request(session, row, owner))
            if action == "admin/statistics":
                rows = (await session.execute(select(BookingRequest.status, func.count()).group_by(BookingRequest.status))).all()
                counts = dict(rows)
                pending = await session.scalar(select(func.count()).select_from(RescheduleRequest).where(RescheduleRequest.status == "pending"))
                return dict(total=sum(counts.values()), approved=counts.get("approved", 0), rejected=counts.get("rejected", 0),
                    pending=counts.get("pending", 0), additional=dict(withdrawn=counts.get("withdrawn", 0)), pending_reschedules=pending)
            if action == "admin/calendar":
                begin = datetime.combine(first, time(), tzinfo=MOSCOW_TZ).astimezone(utc(self.clock()).tzinfo)
                end = datetime.combine(last+timedelta(days=1), time(), tzinfo=MOSCOW_TZ).astimezone(begin.tzinfo)
                # Approved requests appear only through their current meeting time.
                rows = (await session.execute(select(BookingRequest, Meeting).outerjoin(Meeting,
                    Meeting.booking_request_id == BookingRequest.id).where(or_(
                        and_(Meeting.id.is_not(None), Meeting.start_at >= begin, Meeting.start_at < end),
                        and_(Meeting.id.is_(None), BookingRequest.start_at >= begin, BookingRequest.start_at < end)))
                    .order_by(func.coalesce(Meeting.start_at, BookingRequest.start_at), BookingRequest.id)
                    .offset(offset).limit(limit+1))).all()
                items = [await self._request(session, r, owner) for r, m in rows]
            elif action == "admin/inbox":
                # UNION IDs first: bounded pagination across both queues in one snapshot.
                booking = select(BookingRequest.created_at.label("created"), BookingRequest.id.label("identifier"),
                    literal("booking").label("kind")).where(BookingRequest.status == "pending")
                moving = select(RescheduleRequest.created_at.label("created"), RescheduleRequest.id.label("identifier"),
                    literal("reschedule").label("kind")).where(RescheduleRequest.status == "pending")
                union = booking.union_all(moving).subquery()
                query = select(union)
                if kind != "all":
                    query = query.where(union.c.kind == kind)
                rows = (await session.execute(query.order_by(union.c.created, union.c.identifier, union.c.kind).offset(offset).limit(limit+1))).all()
                items = []
                for created, identifier, typ in rows:
                    if typ == "booking":
                        value = await self._request(session, await session.get(BookingRequest, identifier), True)
                    else:
                        p = await session.get(RescheduleRequest, identifier)
                        value = dict(proposal=self.proposal(p), meeting=await self._meeting(session, await session.get(Meeting, p.meeting_id), True))
                    items.append(dict(kind=typ, **value))
            else:
                query = select(BookingRequest)
                if action == "records":
                    query = query.outerjoin(Meeting, Meeting.booking_request_id == BookingRequest.id).where(BookingRequest.client_id == actor.id)
                    upcoming = or_(and_(Meeting.id.is_not(None), Meeting.status.in_(ACTIVE), Meeting.start_at > self.clock()),
                        and_(Meeting.id.is_(None), BookingRequest.status == "pending", BookingRequest.start_at > self.clock()))
                    query = query.where(upcoming if view == "upcoming" else ~upcoming)
                    order = func.coalesce(Meeting.start_at, BookingRequest.start_at)
                    query = query.order_by(order.asc() if view == "upcoming" else order.desc(), BookingRequest.id)
                elif action == "admin/requests":
                    if status != "all":
                        query = query.where(BookingRequest.status == status)
                    query = query.order_by(BookingRequest.created_at.desc(), BookingRequest.id.desc())
                else:
                    raise BookingError("NOT_FOUND")
                rows = (await session.scalars(query.offset(offset).limit(limit+1))).all()
                items = [await self._request(session, row, owner) for row in rows]
            return dict(items=items[:limit], has_more=len(items) > limit)
