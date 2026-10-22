"""Local synthetic QA only; never deploy this fixture server."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiohttp import web
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.config import Settings
from app.db.base import Base
from app.db.models import User, OwnerProfile
from app.db.session import make_engine
from app.services.booking_runtime import build_booking_runtime
from app.web import create_web_app
from tests.test_miniapp_api import signed

async def main():
    path=Path('.deploy/miniapp-qa.db').resolve()
    settings=Settings(_env_file=None,BOT_TOKEN='123:synthetic-qa',OWNER_TELEGRAM_ID=777,
        DATABASE_URL='sqlite+aiosqlite:///'+path.as_posix())
    settings=settings.model_copy(update=dict(miniapp_enabled=True, miniapp_origin='http://127.0.0.1:18713'))
    engine=make_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    now=datetime(2026,10,23,7,tzinfo=timezone.utc)
    async with sessions() as session:
        owner=User(telegram_id=777,full_name='Test owner',role='owner')
        client=User(telegram_id=101,full_name='Test client',role='client',email='test@example.com',consent_at=now)
        session.add_all([owner,client]);await session.flush()
        session.add(OwnerProfile(user_id=owner.id));await session.commit()
    bot=SimpleNamespace(send_message=AsyncMock())
    runtime=build_booking_runtime(sessions,settings=settings,bot=bot,environment='qa',clock=lambda:now,gateway=SimpleNamespace())
    app=create_web_app(settings,sessions,bot,booking_runtime=runtime)
    async def init(request):
        identifier=int(request.match_info['id'])
        return web.json_response(dict(initData=signed(settings,now,identifier=identifier)))
    app.router.add_get('/qa/{id}',init)
    runner=web.AppRunner(app,access_log=None)
    await runner.setup();await web.TCPSite(runner,'127.0.0.1',18713).start()
    print('SYNTHETIC_QA_READY',flush=True)
    try:await asyncio.Event().wait()
    finally:await runner.cleanup();await runtime.close();await engine.dispose()
if __name__ == "__main__":
    asyncio.run(main())
