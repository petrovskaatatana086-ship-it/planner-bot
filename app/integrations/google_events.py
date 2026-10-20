"""Typed, bounded Google HTTP adapter; no response bodies/secrets in exceptions."""
import json
from datetime import datetime
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout
from app.domain.intervals import Interval, utc

BASE = "https://www.googleapis.com/calendar/v3/calendars"


class GoogleFailure(Exception):
    def __init__(self, status, *, retry_after=None, quota=False):
        self.status = status
        self.retry_after = retry_after
        self.quota = quota
        super().__init__(f"GOOGLE_HTTP_{status}")


async def request_json(method, url, **kwargs):
    try:
        async with ClientSession(timeout=ClientTimeout(total=8)) as client:
            async with client.request(method, url, **kwargs) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 1_048_576:
                        raise GoogleFailure(502)
                try:
                    data = json.loads(raw) if raw else {}
                except (ValueError, UnicodeError):
                    if response.status < 400:
                        raise GoogleFailure(502) from None
                    data = {}
                return response.status, data, dict(response.headers)
    except (ClientError, TimeoutError):
        raise GoogleFailure(0) from None


class GoogleEventGateway:
    def __init__(self, token_provider, *, requester=request_json):
        self.token_provider = token_provider
        self.requester = requester

    async def _call(self, method, calendar_id, event_id=None, *, body=None, etag=None):
        url = f"{BASE}/{quote(calendar_id, safe='')}/events"
        if event_id is not None:
            url += f"/{quote(event_id, safe='')}"
        params = {} if method == "GET" else {"sendUpdates": "all"}
        if method in ("POST", "PATCH"):
            params["conferenceDataVersion"] = "1"
        return await self._authenticated(method, url, params, body, etag)

    async def _authenticated(self, method, url, params, body=None, etag=None):
        for refresh in (False, True):
            try:
                token = await self.token_provider(force_refresh=refresh)
            except ValueError:
                raise GoogleFailure(401) from None
            except Exception:
                raise GoogleFailure(0) from None
            headers = {"Authorization": f"Bearer {token}"}
            if etag:
                headers["If-Match"] = etag
            options = dict(headers=headers, params=params)
            if body is not None:
                options["json"] = body
            status, data, response_headers = await self.requester(method, url, **options)
            if status == 401 and not refresh:
                continue
            if status >= 400:
                try:
                    delay = float(response_headers.get("Retry-After", ""))
                except (ValueError, TypeError):
                    delay = None
                error = data.get("error") if isinstance(data, dict) else None
                entries = error.get("errors", []) if isinstance(error, dict) else []
                if not isinstance(entries, list):
                    entries = []
                quota = any(isinstance(e, dict) and e.get("reason") in
                            ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded") for e in entries)
                raise GoogleFailure(status, retry_after=delay, quota=quota)
            if not isinstance(data, dict):
                raise GoogleFailure(502)
            return data
        raise GoogleFailure(401)

    async def busy(self, calendar_id, time_min, time_max):
        """Fail closed on per-calendar errors or incomplete/malformed success bodies."""
        data = await self._authenticated("POST", "https://www.googleapis.com/calendar/v3/freeBusy", {}, {
            "timeMin": utc(time_min).isoformat(), "timeMax": utc(time_max).isoformat(),
            "timeZone": "UTC", "items": [{"id": calendar_id}],
        })
        try:
            parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
            start, end = parse(data["timeMin"]), parse(data["timeMax"])
            if start.tzinfo is None or end.tzinfo is None or start > time_min or end < time_max:
                raise ValueError("incomplete coverage")
            entry = data["calendars"][calendar_id]
            errors = entry.get("errors", []) if isinstance(entry, dict) else None
            if not isinstance(entry, dict) or not isinstance(errors, list) or errors:
                raise ValueError("calendar error")
            periods = entry["busy"]
            if not isinstance(periods, list) or len(periods) > 10000:
                raise ValueError("invalid busy periods")
            result = []
            for period in periods:
                start, end = parse(period["start"]), parse(period["end"])
                if start.tzinfo is None or end.tzinfo is None:
                    raise ValueError("offset required")
                result.append(Interval(start, end))
            return tuple(result)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise GoogleFailure(502) from None

    async def get(self, calendar_id, event_id):
        try:
            return await self._call("GET", calendar_id, event_id)
        except GoogleFailure as error:
            if error.status in (404, 410):
                return None
            raise

    async def insert(self, calendar_id, body):
        return await self._call("POST", calendar_id, body=body)

    async def patch(self, calendar_id, event_id, body, etag):
        return await self._call("PATCH", calendar_id, event_id, body=body, etag=etag)

    async def delete(self, calendar_id, event_id, etag):
        try:
            await self._call("DELETE", calendar_id, event_id, etag=etag)
        except GoogleFailure as error:
            if error.status not in (404, 410):
                raise
