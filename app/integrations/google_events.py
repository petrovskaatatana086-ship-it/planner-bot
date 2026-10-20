"""Typed, bounded Google HTTP adapter; no response bodies/secrets in exceptions."""
import json
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout

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
