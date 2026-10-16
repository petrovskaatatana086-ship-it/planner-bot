from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

from aiohttp import ClientSession

from app.domain.booking import to_moscow

GOOGLE_CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
GOOGLE_FREEBUSY_URL = "https://www.googleapis.com/calendar/v3/freeBusy"
GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"


@dataclass(frozen=True)
class GoogleCalendar:
    id: str
    summary: str
    primary: bool
    access_role: str


@dataclass(frozen=True)
class GoogleBusyPeriod:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class GoogleCreatedEvent:
    event_id: str
    event_url: str | None
    meet_url: str | None


async def list_google_calendars(access_token: str) -> list[GoogleCalendar]:
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"minAccessRole": "owner", "showHidden": "false"}

    async with ClientSession() as session:
        async with session.get(
            GOOGLE_CALENDAR_LIST_URL,
            headers=headers,
            params=params,
            timeout=30,
        ) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                message = data.get("error", {}).get("message") or "Google Calendar list error"
                raise ValueError(message)

    calendars = []
    for item in data.get("items", []):
        calendars.append(
            GoogleCalendar(
                id=item["id"],
                summary=item.get("summary", item["id"]),
                primary=bool(item.get("primary", False)),
                access_role=item.get("accessRole", ""),
            )
        )
    return calendars


async def get_google_busy_periods(
    access_token: str,
    *,
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
) -> list[GoogleBusyPeriod]:
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "items": [{"id": calendar_id}],
    }

    async with ClientSession() as session:
        async with session.post(
            GOOGLE_FREEBUSY_URL,
            headers=headers,
            json=payload,
            timeout=30,
        ) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                message = data.get("error", {}).get("message") or "Google freeBusy error"
                raise ValueError(message)

    busy_items = data.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    return [
        GoogleBusyPeriod(
            start=datetime.fromisoformat(item["start"].replace("Z", "+00:00")),
            end=datetime.fromisoformat(item["end"].replace("Z", "+00:00")),
        )
        for item in busy_items
    ]


def busy_periods_to_slot_starts(
    busy_periods: list[GoogleBusyPeriod],
    slots: list[datetime],
    *,
    duration_minutes: int,
) -> set[datetime]:
    busy_starts: set[datetime] = set()
    duration = timedelta(minutes=duration_minutes)
    for slot in slots:
        slot_end = slot + duration
        for busy in busy_periods:
            if slot < busy.end and slot_end > busy.start:
                busy_starts.add(slot)
                break
    return busy_starts


async def create_google_meeting_event(
    access_token: str,
    *,
    calendar_id: str,
    title: str,
    description: str | None,
    start_at: datetime,
    end_at: datetime,
    attendee_email: str,
    request_id: int,
) -> GoogleCreatedEvent:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = GOOGLE_EVENTS_URL.format(calendar_id=quote(calendar_id, safe=""))
    params = {"conferenceDataVersion": "1", "sendUpdates": "all"}
    payload = {
        "summary": title,
        "description": description or "",
        "start": {"dateTime": to_moscow(start_at).isoformat(), "timeZone": "Europe/Moscow"},
        "end": {"dateTime": to_moscow(end_at).isoformat(), "timeZone": "Europe/Moscow"},
        "attendees": [{"email": attendee_email}],
        "conferenceData": {
            "createRequest": {
                "requestId": f"booking-{request_id}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    async with ClientSession() as session:
        async with session.post(
            url,
            headers=headers,
            params=params,
            json=payload,
            timeout=30,
        ) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                message = data.get("error", {}).get("message") or "Google event create error"
                raise ValueError(message)

    return GoogleCreatedEvent(
        event_id=data["id"],
        event_url=data.get("htmlLink"),
        meet_url=data.get("hangoutLink"),
    )


async def update_google_event_time(
    access_token: str,
    *,
    calendar_id: str,
    event_id: str,
    start_at: datetime,
    end_at: datetime,
) -> None:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = GOOGLE_EVENTS_URL.format(calendar_id=quote(calendar_id, safe="")) + f"/{quote(event_id, safe='')}"
    params = {"sendUpdates": "all"}
    payload = {
        "start": {"dateTime": to_moscow(start_at).isoformat(), "timeZone": "Europe/Moscow"},
        "end": {"dateTime": to_moscow(end_at).isoformat(), "timeZone": "Europe/Moscow"},
    }

    async with ClientSession() as session:
        async with session.patch(
            url,
            headers=headers,
            params=params,
            json=payload,
            timeout=30,
        ) as response:
            if response.status >= 400:
                data = await response.json(content_type=None)
                message = data.get("error", {}).get("message") or "Google event update error"
                raise ValueError(message)


async def cancel_google_event(
    access_token: str,
    *,
    calendar_id: str,
    event_id: str,
) -> None:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = GOOGLE_EVENTS_URL.format(calendar_id=quote(calendar_id, safe="")) + f"/{quote(event_id, safe='')}"
    params = {"sendUpdates": "all"}

    async with ClientSession() as session:
        async with session.delete(
            url,
            headers=headers,
            params=params,
            timeout=30,
        ) as response:
            if response.status not in {200, 204, 410}:
                data = await response.json(content_type=None)
                message = data.get("error", {}).get("message") or "Google event cancel error"
                raise ValueError(message)
