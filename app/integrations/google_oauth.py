import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from aiohttp import ClientSession

from app.config import Settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
)


@dataclass(frozen=True)
class GoogleOAuthLink:
    url: str
    state: str


@dataclass(frozen=True)
class GoogleTokenResponse:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str
    token_type: str
    id_token: str | None


def google_oauth_is_configured(settings: Settings) -> bool:
    return bool(
        settings.google_client_id
        and settings.google_client_secret
        and settings.google_redirect_uri
    )


def build_google_oauth_link(settings: Settings, owner_user_id: int) -> GoogleOAuthLink:
    state = f"{owner_user_id}:{secrets.token_urlsafe(24)}"
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "include_granted_scopes": "true",
    }
    return GoogleOAuthLink(url=f"{GOOGLE_AUTH_URL}?{urlencode(params)}", state=state)


def parse_oauth_state(state: str) -> tuple[int, str] | None:
    owner_id, separator, nonce = state.partition(":")
    if not separator or not owner_id.isdigit() or not nonce:
        return None
    return int(owner_id), nonce


async def exchange_code_for_tokens(settings: Settings, code: str) -> GoogleTokenResponse:
    payload = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
        "grant_type": "authorization_code",
    }
    async with ClientSession() as session:
        async with session.post(GOOGLE_TOKEN_URL, data=payload, timeout=30) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                message = data.get("error_description") or data.get("error") or "Google token error"
                raise ValueError(message)

    expires_at = None
    if data.get("expires_in"):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(data["expires_in"]))

    return GoogleTokenResponse(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=expires_at,
        scope=data.get("scope", ""),
        token_type=data.get("token_type", "Bearer"),
        id_token=data.get("id_token"),
    )


async def refresh_access_token(settings: Settings, refresh_token: str) -> GoogleTokenResponse:
    payload = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with ClientSession() as session:
        async with session.post(GOOGLE_TOKEN_URL, data=payload, timeout=30) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                message = data.get("error_description") or data.get("error") or "Google refresh error"
                raise ValueError(message)

    expires_at = None
    if data.get("expires_in"):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(data["expires_in"]))

    return GoogleTokenResponse(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=expires_at,
        scope=data.get("scope", ""),
        token_type=data.get("token_type", "Bearer"),
        id_token=data.get("id_token"),
    )
