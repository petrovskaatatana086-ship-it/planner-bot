"""HTTP boundary: identity only from verified Telegram, explicit versions/keys."""
import base64
from collections import OrderedDict, deque
from datetime import date, datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import time
import uuid

from aiohttp import web
from sqlalchemy import select

from app.db.models import OperationResult
from app.db.transactions import DatabaseBusy
from app.domain.registration import CONSENT_TEXT, CONSENT_VERSION
from app.services.booking import BookingError
from app.services.miniapp_auth import MiniAppAuth
from app.services.miniapp_queries import MiniAppQueries, stamp
from app.services.outbox import DeliveryError
from app.services.profiles import SETTING_FIELDS

PREFIX = "/api/meetly/v1"
ASSETS = Path(__file__).resolve().parent.parent / "miniapp"
MESSAGES = {
    "INVALID_AUTH": "Откройте приложение из Telegram.", "SESSION_EXPIRED": "Закройте и заново откройте приложение в Telegram.",
    "ACCOUNT_BLOCKED": "Доступ ограничен. Обратитесь к специалисту.", "FORBIDDEN": "Нет доступа к этому действию.",
    "REGISTRATION_REQUIRED": "Сначала заполните профиль и примите согласие.", "NOT_FOUND": "Запись не найдена.",
    "INVALID_REQUEST": "Проверьте запрос.", "VALIDATION_FAILED": "Проверьте заполненные поля.",
    "VERSION_REQUIRED": "Обновите карточку перед изменением.", "STALE_VERSION": "Данные изменились. Обновите раздел.",
    "INVALID_STATE": "Действие недоступно. Обновите раздел.", "IDEMPOTENCY_KEY_REUSED": "Обновите раздел и повторите действие.",
    "SLOT_UNAVAILABLE": "Это время уже занято. Выберите другое.", "OUTSIDE_SCHEDULE": "Это время вне расписания. Выберите доступный слот.",
    "OUTSIDE_HORIZON": "Дата вне периода записи.", "NOTICE_REQUIRED": "До встречи осталось слишком мало времени.",
    "DAILY_LIMIT_REACHED": "На этот день достигнут лимит встреч.", "CONSENT_CHANGED": "Текст согласия обновился. Откройте форму заново.",
    "CALENDAR_UNAVAILABLE": "Не удалось проверить календарь. Повторите позже.", "DATABASE_BUSY": "Сервис занят. Повторите через несколько секунд.",
    "BOOKING_DISABLED": "Специалист временно выключил запись.", "BOOKING_UNAVAILABLE": "Запись временно недоступна.",
    "RATE_LIMITED": "Слишком много запросов. Подождите минуту.", "INTERNAL_ERROR": "Не удалось выполнить действие. Повторите позже.",
}


def status_for(code):
    if code in ("INVALID_AUTH", "SESSION_EXPIRED"): return 401
    if code in ("ACCOUNT_BLOCKED", "FORBIDDEN", "REGISTRATION_REQUIRED"): return 403
    if code == "NOT_FOUND": return 404
    if code == "VERSION_REQUIRED": return 428
    if code == "RATE_LIMITED": return 429
    if code in ("DATABASE_BUSY", "CALENDAR_UNAVAILABLE", "BOOKING_UNAVAILABLE", "MAINTENANCE"): return 503
    if code in ("VALIDATION_FAILED", "OUTSIDE_SCHEDULE", "OUTSIDE_HORIZON", "NOTICE_REQUIRED", "DAILY_LIMIT_REACHED", "INVALID_SCHEDULE"): return 422
    if code in ("INVALID_REQUEST",): return 400
    if code == "INTERNAL_ERROR": return 500
    return 409


class RateLimits:
    def __init__(self): self.buckets = OrderedDict()
    def check(self, key, maximum):
        now = time.monotonic()
        bucket = self.buckets.pop(key, deque())
        while bucket and bucket[0] <= now-60: bucket.popleft()
        self.buckets[key] = bucket
        while len(self.buckets) > 4096: self.buckets.popitem(last=False)
        if len(bucket) >= maximum: raise BookingError("RATE_LIMITED")
        bucket.append(now)


def response(data, request, status=200, **headers):
    return web.json_response(dict(data=data, request_id=request["request_id"]), status=status, headers=headers)


def install_miniapp(app, settings, sessions, runtime):
    api = MiniAppAPI(settings, sessions, runtime)
    @web.middleware
    async def boundary(request, handler):
        if not (request.path.startswith(PREFIX + "/") or request.path.startswith("/miniapp")):
            return await handler(request)
        request["request_id"] = str(uuid.uuid4())
        try:
            if request.headers.get("Origin", settings.miniapp_origin) != settings.miniapp_origin:
                raise BookingError("FORBIDDEN")
            result = await handler(request)
        except (BookingError, DatabaseBusy, DeliveryError) as error:
            code = getattr(error, "code", "DATABASE_BUSY")
            if code == "RESOURCE_NOT_FOUND": code = "NOT_FOUND"
            status = status_for(code)
            result = web.json_response(dict(error=dict(code=code, message=MESSAGES.get(code, "Действие недоступно. Обновите раздел."),
                retryable=status in (429, 503)), request_id=request["request_id"]), status=status)
            if status in (429, 503): result.headers["Retry-After"] = "60" if status == 429 else "3"
        except web.HTTPException as error:
            result = web.json_response(dict(error=dict(code="INVALID_REQUEST", message="Проверьте запрос.", retryable=False),
                request_id=request["request_id"]), status=error.status)
        except Exception:
            # No raw requests, auth headers, bodies or external exception text in logs.
            result = web.json_response(dict(error=dict(code="INTERNAL_ERROR", message=MESSAGES["INTERNAL_ERROR"], retryable=False),
                request_id=request["request_id"]), status=500)
        result.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
        if request.path.startswith("/miniapp"):
            result.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors https://web.telegram.org https://*.telegram.org")
        return result
    app.middlewares.append(boundary)
    app.router.add_route("*", PREFIX + "/{tail:.*}", api.handle)
    async def redirect(request):
        raise web.HTTPFound("./miniapp/")
    app.router.add_get("/miniapp", redirect)
    app.router.add_get("/miniapp/{asset:.*}", api.asset)


class MiniAppAPI:
    def __init__(self, settings, sessions, runtime):
        self.settings, self.sessions, self.runtime = settings, sessions, runtime
        self.clock = runtime.commands.clock
        self.auth = MiniAppAuth(sessions, settings, clock=self.clock)
        self.queries = MiniAppQueries(sessions, settings, self.clock)
        self.limits = RateLimits()

    async def asset(self, request):
        asset = request.match_info["asset"] or "live.html"
        if asset not in {"live.html", "live.js", "live.css", "styles.css", "registration.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(ASSETS / asset)

    @staticmethod
    def token(request, scheme):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith(scheme + " ") or not auth[len(scheme)+1:]: raise BookingError("INVALID_AUTH")
        return auth[len(scheme)+1:]

    @staticmethod
    async def body(request, required=(), optional=()):
        if request.content_type != "application/json": raise BookingError("INVALID_REQUEST")
        def unique(pairs):
            value = dict(pairs)
            if len(value) != len(pairs): raise ValueError
            return value
        try:
            chunks, length = [], 0
            async for chunk in request.content.iter_chunked(8192):
                length += len(chunk)
                if length > 32768: raise ValueError
                chunks.append(chunk)
            raw = b"".join(chunks)
            data = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(data, dict) or set(data)-set(required)-set(optional) or set(required)-set(data): raise ValueError
            return data
        except (ValueError, UnicodeError): raise BookingError("INVALID_REQUEST") from None

    @staticmethod
    def integer(value):
        if type(value) is not int or value <= 0: raise BookingError("VALIDATION_FAILED")
        return value

    @staticmethod
    def start(value):
        try:
            if not isinstance(value, str) or len(value) > 40: raise ValueError
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None: raise ValueError
            return result
        except ValueError: raise BookingError("VALIDATION_FAILED") from None

    @staticmethod
    def version(request):
        value = request.headers.get("If-Match", "")
        if not re.fullmatch(r'"[1-9][0-9]{0,9}"', value): raise BookingError("VERSION_REQUIRED")
        return int(value[1:-1])

    def cursor(self, request, actor, value=None):
        context = json.dumps([actor.id, request.path, sorted((k,v) for k,v in request.query.items() if k != "cursor")], separators=(",", ":"))
        key = hashlib.sha256(("meetly-cursor:"+self.settings.bot_token).encode()).digest()
        if value is not None:
            payload = str(value)
            signature = hmac.new(key, (context+":"+payload).encode(), hashlib.sha256).hexdigest()
            return base64.urlsafe_b64encode((payload+":"+signature).encode()).decode().rstrip("=")
        raw = request.query.get("cursor")
        if raw is None: return 0
        try:
            if len(raw) > 180: raise ValueError
            payload, signature = base64.urlsafe_b64decode(raw+"="*(-len(raw)%4)).decode().split(":")
            if not re.fullmatch(r"[0-9]{1,7}", payload): raise ValueError
            expected = hmac.new(key, (context+":"+payload).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected): raise ValueError
            return int(payload)
        except (ValueError, UnicodeError): raise BookingError("INVALID_REQUEST") from None

    async def handle(self, request):
        tail, method = request.match_info["tail"], request.method
        if method != "GET" and request.query:
            raise BookingError("INVALID_REQUEST")
        if tail in ("me", "consent", "session") and request.query:
            raise BookingError("INVALID_REQUEST")
        if tail == "session" and method == "POST":
            self.limits.check(("login", request.remote), 10)
            if request.can_read_body: raise BookingError("INVALID_REQUEST")
            return response(await self.auth.exchange(self.token(request, "tma")), request)
        token = self.token(request, "Bearer")
        if tail == "session" and method == "DELETE":
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token): raise BookingError("INVALID_AUTH")
            self.limits.check(("logout", request.remote), 30)
            await self.auth.revoke(token)
            return web.Response(status=204)
        actor = await self.auth.resolve(token)
        owner = actor.telegram_id == self.settings.owner_telegram_id
        self.limits.check(("read" if method == "GET" else "write", actor.id), 120 if method == "GET" else 20)
        if tail == "me" and method == "GET": return response(await self.queries.read(actor.id, "me"), request)
        if tail == "consent" and method == "GET": return response(dict(version=CONSENT_VERSION, text=CONSENT_TEXT), request)
        if tail == "registration" and method == "POST":
            key = self.command_key(request)
            data = await self.body(request, ("full_name", "email", "consent", "consent_version"))
            prior = await self.prior(actor.id, key)
            result = await self.auth.profiles.register(actor_id=actor.id, command_id=key, **data)
            return response(result, request, 201, **({"Idempotency-Replayed":"true"} if prior else {}))
        if tail.startswith("admin/") and not owner: raise BookingError("FORBIDDEN")
        if not owner and not (actor.email and actor.consent_at): raise BookingError("REGISTRATION_REQUIRED")
        if method == "GET": return await self.get(request, actor, tail)
        if tail == "admin/settings" and method == "PATCH":
            key = self.command_key(request)
            prior = await self.prior(actor.id, key)
            data = await self.body(request, optional=SETTING_FIELDS)
            result = await self.auth.profiles.patch_settings(actor_id=actor.id, command_id=key, changes=data, expected_version=self.version(request))
            return response(result, request, **({"Idempotency-Replayed":"true"} if prior else {}))
        if method != "POST": raise web.HTTPMethodNotAllowed(method, ["GET", "POST", "PATCH", "DELETE"])
        key = self.command_key(request)
        prior = await self.prior(actor.id, key)
        payload = {}
        status = 200
        if tail == "requests":
            if owner: raise BookingError("FORBIDDEN")
            action = "create_request"
            payload = await self.body(request, ("start_at", "title", "settings_version"), ("comment",))
            status = 201
        else:
            match = re.fullmatch(r"(admin/)?(requests|meetings)/([1-9][0-9]{0,9})/([a-z-]+)", tail)
            if not match: raise BookingError("NOT_FOUND")
            admin, resource, identifier, verb = match.groups()
            if bool(admin) != owner: raise BookingError("FORBIDDEN")
            routes = {
                (True,"requests","approve"): ("approve_request", (), ()),
                (True,"requests","reject"): ("reject_request", ("reason",), ()),
                (True,"meetings","approve-reschedule"): ("approve_reschedule", ("reschedule_request_id",), ()),
                (True,"meetings","reject-reschedule"): ("reject_reschedule", ("reschedule_request_id","reason"), ()),
                (True,"meetings","reschedule"): ("reschedule_by_owner", ("start_at","settings_version"), ("reason",)),
                (False,"meetings","reschedule-requests"): ("request_reschedule", ("start_at","settings_version"), ("comment",)),
                (True,"meetings","cancel"): ("cancel_meeting", (), ("reason",)),
                (False,"meetings","cancel"): ("cancel_meeting", (), ("reason",)),
            }
            route = routes.get((bool(admin),resource,verb))
            if route is None: raise BookingError("NOT_FOUND")
            action, required, optional = route
            payload = await self.body(request, required, optional)
            payload.update({"request_id" if resource == "requests" else "meeting_id": int(identifier), "expected_version": self.version(request)})
            if action == "request_reschedule": status = 201
        for field in ("settings_version", "reschedule_request_id"):
            if field in payload: payload[field] = self.integer(payload[field])
        if "start_at" in payload: payload["start_at"] = self.start(payload["start_at"])
        result = await getattr(self.runtime.commands, action)(actor_id=actor.id, command_id=key, **payload)
        return response(result, request, status, **({"Idempotency-Replayed":"true"} if prior else {}))

    @staticmethod
    def command_key(request):
        raw = request.headers.get("Idempotency-Key", "")
        try:
            key = uuid.UUID(raw)
            if str(key) != raw.lower(): raise ValueError
            return "api:" + str(key)
        except ValueError: raise BookingError("INVALID_REQUEST") from None

    async def prior(self, actor_id, key):
        async with self.sessions() as session:
            return await session.scalar(select(OperationResult).where(OperationResult.actor_id == actor_id, OperationResult.command_id == key))

    async def get(self, request, actor, tail):
        if tail.startswith("operations/"):
            try: key = "api:"+str(uuid.UUID(tail.split("/",1)[1]))
            except ValueError: raise BookingError("INVALID_REQUEST") from None
            prior = await self.prior(actor.id, key)
            if prior is None: raise BookingError("NOT_FOUND")
            created = prior.kind in ("request.created", "meeting.reschedule_requested", "registration.completed")
            return response(dict(resource_id=prior.resource_id, resource_type=prior.kind,
                status_code=201 if created else 200, response=json.loads(prior.response_json)), request)
        allowed = {"availability/days": {"from","to","meeting_id"}, "availability/slots": {"date","meeting_id"},
            "records": {"view","limit","cursor"}, "admin/requests": {"status","limit","cursor"},
            "admin/calendar": {"from","to","limit","cursor"}, "admin/inbox": {"kind","limit","cursor"}}
        q = request.query
        if len(q) != len(set(q.keys())) or set(q)-allowed.get(tail,set()): raise BookingError("INVALID_REQUEST")
        def day(key):
            try:
                value = q.get(key, "")
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value): raise ValueError
                return date.fromisoformat(value)
            except ValueError: raise BookingError("INVALID_REQUEST") from None
        if tail.startswith("availability/"):
            meeting_id = q.get("meeting_id")
            if meeting_id is not None:
                if not re.fullmatch(r"[1-9][0-9]{0,9}", meeting_id): raise BookingError("INVALID_REQUEST")
                meeting_id = int(meeting_id)
            args = dict(actor_id=actor.id, meeting_id=meeting_id)
            if tail == "availability/days": args.update(from_date=day("from"), to_date=day("to")); action="days"
            elif tail == "availability/slots": args.update(date=day("date")); action="slots"
            else: raise BookingError("NOT_FOUND")
            view = await getattr(self.runtime.availability_queries, action)(**args)
            return response(dict(settings_version=view.settings_version, checked_at=stamp(view.checked_at), timezone=view.timezone,
                duration_minutes=view.duration_minutes, horizon_end=view.horizon_end.isoformat(),
                days=[dict(date=x.date.isoformat(), available_slots_count=x.available_slots_count) for x in view.days],
                slots=[dict(start_at=stamp(x.start), end_at=stamp(x.end)) for x in view.slots]), request)
        args = {}
        detail = re.fullmatch(r"((?:admin/)?(?:requests|meetings))/([1-9][0-9]{0,9})", tail)
        if detail: tail=detail[1]+"/detail"; args["identifier"]=int(detail[2])
        if tail == "admin/calendar":
            first, last = day("from"), day("to")
            if not 0 <= (last-first).days <= 30: raise BookingError("INVALID_REQUEST")
            args.update(first=first,last=last)
        for field, choices, default in (("status", {"all","pending","approved","rejected","withdrawn"}, "all"),
                ("view", {"upcoming","history"}, "upcoming"), ("kind", {"all","booking","reschedule"}, "all")):
            value = q.get(field,default)
            if value not in choices: raise BookingError("INVALID_REQUEST")
            args[field] = value
        try:
            if not re.fullmatch(r"[0-9]{1,3}",q.get("limit","20")): raise ValueError
            limit=int(q.get("limit","20"))
            if not 1 <= limit <= 100: raise ValueError
        except ValueError: raise BookingError("INVALID_REQUEST") from None
        offset=self.cursor(request,actor)
        result = await self.queries.read(actor.id,tail,limit=limit,offset=offset,**args)
        if "has_more" in result:
            result["next_cursor"] = self.cursor(request,actor,offset+limit) if result.pop("has_more") else None
        return response(result,request)
