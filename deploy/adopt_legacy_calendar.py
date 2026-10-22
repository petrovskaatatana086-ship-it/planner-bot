"""Maintenance-only legacy adoption. Inspect writes a plan; apply adds private tags only."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.models import User
from app.db.session import make_engine
from app.integrations.google_events import GoogleEventGateway, GoogleFailure
from app.services.booking import BookingError
from app.services.booking_runtime import OwnerGoogleTokens, verify_booking_schema
from app.services.calendar_adoption import AdoptionPlan, CalendarAdoption


async def run(args):
    database = args.database.resolve(strict=True)
    env_file = args.env_file.resolve(strict=True)
    if not database.is_file() or not env_file.is_file():
        raise ValueError("Invalid database")
    settings = Settings(_env_file=env_file, BOT_TOKEN="0:maintenance", OWNER_TELEGRAM_ID=args.owner_telegram_id,
        DATABASE_URL=f"sqlite+aiosqlite:///{database.as_posix()}", BOOKING_RUNTIME_ENABLED=False,
        CALENDAR_ENVIRONMENT=args.environment)
    engine = make_engine(settings)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await verify_booking_schema(sessions)
        async with sessions() as session:
            actor_id = await session.scalar(select(User.id).where(User.telegram_id == settings.owner_telegram_id))
        service = CalendarAdoption(sessions, gateway=GoogleEventGateway(OwnerGoogleTokens(sessions, settings=settings)),
            owner_telegram_id=settings.owner_telegram_id, environment=args.environment)
        if args.action == "inspect":
            plan = await service.inspect(actor_id=actor_id, meeting_id=args.meeting_id)
            with args.plan.open("x", encoding="utf-8") as target:
                json.dump(asdict(plan), target, indent=2)
                target.write("\n")
            return dict(state="review_required", meeting_id=plan.meeting_id)
        plan = AdoptionPlan(**json.loads(args.plan.read_text(encoding="utf-8")))
        return await service.adopt(actor_id=actor_id, plan=plan)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "apply"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True, help="Explicit OAuth configuration; never inferred")
    parser.add_argument("--owner-telegram-id", type=int, required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--meeting-id", type=int)
    args = parser.parse_args()
    if args.action == "inspect" and not args.meeting_id:
        parser.error("inspect requires --meeting-id")
    try:
        result = asyncio.run(run(args))
    except BookingError as error:
        result = dict(error_code=error.code)
    except GoogleFailure:
        result = dict(error_code="CALENDAR_UNAVAILABLE")
    except Exception:
        result = dict(error_code="MAINTENANCE_FAILED")
    print(json.dumps(result))
    return 1 if "error_code" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
