import hashlib
import hmac
import json
from datetime import timedelta
import unittest
import uuid
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select, func

from app.db.models import ApiSession, BookingRequest, Meeting, User
from app.services.booking import BookingError
from app.services.miniapp_auth import validate_init_data
from app.web import create_web_app
from tests import test_bot_booking as fixtures


def signed(settings, now, *, identifier=101, **extra):
    fields = dict(auth_date=str(int(now.timestamp())), user=json.dumps(dict(id=identifier, first_name="Test")), **extra)
    secret = hmac.digest(b"WebAppData", settings.bot_token.encode(), "sha256")
    fields["hash"] = hmac.new(secret, "\n".join(f"{k}={v}" for k,v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class MiniAppApiTest(unittest.IsolatedAsyncioTestCase):
    create = fixtures.BotBookingTest.create
    count = fixtures.BotBookingTest.count
    meeting = fixtures.BotBookingTest.meeting

    async def asyncSetUp(self):
        await fixtures.BotBookingTest.asyncSetUp(self)
        self.settings = self.settings.model_copy(update=dict(miniapp_enabled=True, miniapp_origin="https://meetly.example"))
        self.client_http = TestClient(TestServer(create_web_app(self.settings, self.sessions, self.bot, booking_runtime=self.runtime)))
        await self.client_http.start_server()
        self.url = "/api/meetly/v1/"
        self.client_token = await self.login(101)
        self.owner_token = await self.login(777)

    async def asyncTearDown(self):
        await self.client_http.close()
        await fixtures.BotBookingTest.asyncTearDown(self)

    async def login(self, identifier):
        response = await self.client_http.post(self.url+"session", headers={"Authorization":"tma "+signed(self.settings,self.now,identifier=identifier)})
        self.assertEqual(response.status,200,await response.text())
        return (await response.json())["data"]["token"]

    async def test_past_pending_request_explains_why_approval_is_unavailable(self):
        created = await self.create()
        path = f"admin/requests/{created['id']}"
        response = await self.call("GET", path, owner=True)
        self.assertIn("approve", (await response.json())["data"]["allowed_actions"])
        async with self.sessions() as session:
            row = await session.get(BookingRequest, created["id"])
            row.start_at = self.now
            row.end_at = self.now + timedelta(hours=1)
            await session.commit()
        response = await self.call("GET", path, owner=True)
        data = (await response.json())["data"]
        self.assertEqual(data["allowed_actions"], ["reject"])
        self.assertEqual(data["approval_unavailable_reason"], "REQUEST_IN_PAST")
        response = await self.call("POST", path+"/approve", owner=True, body={},
            version=1, key=str(uuid.uuid4()))
        self.assertEqual(response.status, 409)
        self.assertIn("время встречи прошло", (await response.json())["error"]["message"])
        self.assertEqual(await self.count(Meeting), 0)

    async def test_closed_working_day_blocks_direct_booking_and_settings_roundtrip(self):
        response = await self.call("GET", "admin/settings", owner=True)
        settings = (await response.json())["data"]
        response = await self.call("PATCH", "admin/settings", owner=True,
            body={"working_days": []}, version=settings["version"], key=str(uuid.uuid4()))
        self.assertEqual(response.status, 200, await response.text())
        response = await self.call("GET", "admin/settings", owner=True)
        self.assertEqual((await response.json())["data"]["working_days"], [])
        with self.assertRaises(BookingError) as error:
            await self.adapter.execute("create_request", actor_id=self.client.id,
                command_id=uuid.uuid4().hex, start_at=self.start, title="Closed day", settings_version=2)
        self.assertEqual(error.exception.code, "OUTSIDE_SCHEDULE")

    async def call(self, method, path, *, owner=False, token=None, body=None, version=None, key=None, headers=None):
        hdr = {"Authorization":"Bearer "+(token or (self.owner_token if owner else self.client_token))}
        if version is not None: hdr["If-Match"]=f'"{version}"'
        if key: hdr["Idempotency-Key"]=key
        hdr.update(headers or {})
        return await self.client_http.request(method,self.url+path,headers=hdr,**({"json":body} if body is not None else {}))

    async def test_telegram_hmac_tampering_age_duplicates_signature_and_roles(self):
        raw = signed(self.settings,self.now,signature="signed-third-party-field")
        self.assertEqual(validate_init_data(raw,self.settings.bot_token,self.now)["telegram_id"],101)
        for invalid in (raw+"&user=x", raw.replace("101","777"), raw+"&x=%GG",
                signed(self.settings,self.now-timedelta(seconds=301)), signed(self.settings,self.now+timedelta(seconds=31)),
                signed(self.settings,self.now,identifier=True)):
            with self.subTest(value="invalid-vector"):
                with self.assertRaises(BookingError): validate_init_data(invalid,self.settings.bot_token,self.now)
        me=await self.call("GET","me")
        self.assertEqual((await me.json())["data"]["role"],"client")
        me=await self.call("GET","me",owner=True)
        self.assertEqual((await me.json())["data"]["role"],"owner")

    async def test_session_revocation_expiry_block_origin_and_no_secrets(self):
        response=await self.call("GET","me",headers={"Origin":"https://evil.example"})
        self.assertEqual(response.status,403)
        response=await self.call("GET","me")
        self.assertEqual(response.headers["Cache-Control"],"no-store")
        self.assertNotIn("token_hash",await response.text())
        async with self.sessions() as session:
            rows=(await session.scalars(select(ApiSession))).all()
            self.assertEqual(len(rows),2)
            self.assertTrue(all(row.token_hash != self.client_token for row in rows))
            (await session.get(User,self.client.id)).status="blocked"
            await session.commit()
        self.assertEqual((await self.call("GET","me")).status,403)
        self.assertEqual((await self.call("DELETE","session")).status,204)
        self.assertEqual((await self.call("DELETE","session")).status,204)
        self.assertEqual((await self.call("GET","me")).status,401)
        self.runtime.commands.clock=lambda:self.now+timedelta(hours=2)
        # Auth receives its own clock reference; alter database expiry deterministically.
        async with self.sessions() as session:
            for row in (await session.scalars(select(ApiSession))).all(): row.expires_at=self.now-timedelta(seconds=1)
            await session.commit()
        self.assertEqual((await self.call("GET","me",owner=True)).status,401)

    async def test_read_routes_pagination_ownership_and_safe_dto(self):
        meeting=await self.meeting()
        for path in ("me","consent","booking-rules","availability/days?from=2026-10-23&to=2026-11-01",
                "availability/slots?date=2026-10-27", "records?view=upcoming", "records?view=history",
                f"requests/{meeting.booking_request_id}",f"meetings/{meeting.id}"):
            with self.subTest(path=path):
                response=await self.call("GET",path)
                self.assertEqual(response.status,200,await response.text())
                self.assertNotIn("client",(await response.json())["data"])
        for path in ("admin/calendar?from=2026-10-23&to=2026-11-01","admin/inbox","admin/requests",
                "admin/statistics","admin/settings",f"admin/meetings/{meeting.id}"):
            with self.subTest(path=path):
                response=await self.call("GET",path,owner=True)
                self.assertEqual(response.status,200,await response.text())
                self.assertEqual((await self.call("GET",path)).status,403)
        foreign=await self.login(909)
        self.assertEqual((await self.call("GET",f"meetings/{meeting.id}",token=foreign)).status,403)
        async with self.sessions() as session:
            row=await session.scalar(select(User).where(User.telegram_id==909))
            row.email="foreign@example.com";row.consent_at=self.now
            await session.commit()
        self.assertEqual((await self.call("GET",f"meetings/{meeting.id}",token=foreign)).status,404)
        self.assertEqual((await self.call("GET",f"availability/slots?date=2026-10-27&meeting_id={meeting.id}",token=foreign)).status,404)
        for path in ("records?limit=101","records?view=bad","records?actor_id=777","records?view=history&view=upcoming",
                "admin/calendar?from=2026-10-01&to=2026-12-01"):
            self.assertEqual((await self.call("GET",path,owner=path.startswith("admin/"))).status,400)

    async def test_full_client_request_owner_decision_reschedule_reject_cancel_replay(self):
        key=str(uuid.uuid4())
        body=dict(start_at="2026-10-26T10:00:00+03:00",title="Test <script>",settings_version=1)
        response=await self.call("POST","requests",body=body,key=key)
        self.assertEqual(response.status,201,await response.text()); request_id=(await response.json())["data"]["id"]
        repeated=await self.call("POST","requests",body=body,key=key)
        self.assertEqual(repeated.headers["Idempotency-Replayed"],"true")
        self.assertEqual((await repeated.json())["data"]["id"],request_id)
        self.assertEqual((await self.call("POST","requests",body={**body,"title":"Changed"},key=key)).status,409)
        self.assertEqual((await self.call("POST",f"admin/requests/{request_id}/approve",owner=True,body={},key=str(uuid.uuid4()))).status,428)
        response=await self.call("POST",f"admin/requests/{request_id}/approve",owner=True,body={},key=str(uuid.uuid4()),version=1)
        self.assertEqual(response.status,200,await response.text()); meeting_id=(await response.json())["data"]["meeting_id"]
        response=await self.call("POST",f"meetings/{meeting_id}/reschedule-requests",body=dict(start_at="2026-10-27T11:00:00+03:00",settings_version=1),key=str(uuid.uuid4()),version=1)
        self.assertEqual(response.status,201,await response.text()); proposal=(await response.json())["data"]["reschedule_request_id"]
        response=await self.call("GET","admin/inbox",owner=True)
        self.assertEqual(response.status,200,await response.text());self.assertEqual((await response.json())["data"]["items"][0]["kind"],"reschedule")
        response=await self.call("POST",f"admin/meetings/{meeting_id}/reject-reschedule",owner=True,body=dict(reschedule_request_id=proposal,reason="Other time"),key=str(uuid.uuid4()),version=2)
        self.assertEqual(response.status,200,await response.text())
        response=await self.call("POST",f"admin/meetings/{meeting_id}/reschedule",owner=True,body=dict(start_at="2026-10-28T11:00:00+03:00",settings_version=1),key=str(uuid.uuid4()),version=3)
        self.assertEqual(response.status,200,await response.text())
        cancel_key=str(uuid.uuid4())
        response=await self.call("POST",f"meetings/{meeting_id}/cancel",body={},key=cancel_key,version=4)
        self.assertEqual(response.status,200,await response.text())
        response=await self.call("POST",f"meetings/{meeting_id}/cancel",body={},key=cancel_key,version=4)
        self.assertEqual(response.status,200,await response.text())
        self.assertEqual(response.headers["Idempotency-Replayed"],"true")
        operation=await self.call("GET","operations/"+cancel_key)
        self.assertEqual((await operation.json())["data"]["response"]["status"],"cancelled_by_client")
        self.assertEqual((await self.call("GET","operations/"+cancel_key,owner=True)).status,404)

    async def test_registration_settings_schema_and_static(self):
        token=await self.login(808)
        response=await self.call("GET","me",token=token)
        self.assertTrue((await response.json())["data"]["registration_required"])
        body=dict(full_name="New Client",email="new@example.com",consent=True,consent_version="bot-consent-v1")
        response=await self.call("POST","registration",token=token,body=body,key=str(uuid.uuid4()))
        self.assertEqual(response.status,201,await response.text())
        response=await self.call("PATCH","admin/settings",owner=True,body=dict(default_duration=45),key=str(uuid.uuid4()),version=1)
        self.assertEqual(response.status,200,await response.text())
        self.assertEqual((await response.json())["data"]["version"],2)
        for body in ({"booking_enabled":"yes"},{"unknown":123}):
            response=await self.call("PATCH","admin/settings",owner=True,body=body,key=str(uuid.uuid4()),version=2)
            self.assertIn(response.status,(400,422))
        for path in ("/miniapp/","/miniapp/live.js","/miniapp/styles.css"):
            response=await self.client_http.get(path)
            self.assertEqual(response.status,200,await response.text())
            self.assertIn("frame-ancestors",response.headers["Content-Security-Policy"])
        self.assertEqual((await self.client_http.get("/miniapp/app.js")).status,404)
        self.assertEqual((await self.client_http.get("/miniapp/../.env")).status,404)

    async def test_rate_limit_and_unknown_fields_reject_actor_spoof(self):
        for i in range(11):
            response=await self.client_http.post(self.url+"session",headers={"Authorization":"tma invalid"})
        self.assertEqual(response.status,429)
        self.assertEqual(response.headers["Retry-After"],"60")
        response=await self.call("POST","requests",body=dict(start_at="2026-10-26T10:00:00+03:00",title="Test",settings_version=1,actor_id=777),key=str(uuid.uuid4()))
        self.assertEqual(response.status,400)

    async def test_cursor_binding_limits_and_json_body_boundary(self):
        await self.create()
        await self.adapter.execute("create_request", actor_id=self.client.id, command_id="cursor-second",
            start_at=self.start+timedelta(hours=2), title="Second request", settings_version=1)
        response=await self.call("GET","records?view=upcoming&limit=1")
        first=(await response.json())["data"]
        self.assertEqual(len(first["items"]),1)
        self.assertIsNotNone(first["next_cursor"])
        cursor=first["next_cursor"]
        response=await self.call("GET","records?view=upcoming&limit=1&cursor="+cursor)
        second=(await response.json())["data"]
        self.assertNotEqual(first["items"][0]["id"],second["items"][0]["id"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual((await self.call("GET","records?view=history&limit=1&cursor="+cursor)).status,400)
        self.assertEqual((await self.call("GET","records?view=upcoming&limit=1&cursor="+cursor+"bad")).status,400)
        headers={"Authorization":"Bearer "+self.client_token,"Content-Type":"application/json","Idempotency-Key":str(uuid.uuid4())}
        for raw in ('{"title":"one","title":"two"}', '{"title":"'+"x"*33000+'"}'):
            response=await self.client_http.post(self.url+"requests",data=raw,headers=headers)
            self.assertEqual(response.status,400)
