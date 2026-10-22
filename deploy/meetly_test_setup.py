"""Prepare an isolated test backend from .env.local; no network or production writes."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil

from dotenv import dotenv_values

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]


class SetupError(Exception):
    pass


def prepare(*, root=ROOT, destination=None, image, host_port=8013):
    root = Path(root).resolve(strict=True)
    if not isinstance(image, str) or not re.fullmatch(r"meetly-test:[a-f0-9]{40,64}", image):
        raise SetupError("PINNED_TEST_IMAGE_REQUIRED")
    if type(host_port) is not int or not 1024 <= host_port <= 65535 or host_port in (8012, 3107, 3110):
        raise SetupError("ISOLATED_PORT_REQUIRED")
    values = dotenv_values(root/".env.local", interpolate=False, encoding="utf-8-sig")
    token = values.get("BOT_TOKEN") or ""
    if not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]{20,}", token):
        raise SetupError("SEPARATE_TEST_TOKEN_REQUIRED")
    production = dotenv_values(root/".env", interpolate=False, encoding="utf-8-sig")
    if not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]{20,}", production.get("BOT_TOKEN") or ""):
        raise SetupError("PRODUCTION_IDENTITY_REFERENCE_REQUIRED")
    if token.split(":", 1)[0] == (production.get("BOT_TOKEN") or "").split(":", 1)[0]:
        raise SetupError("PRODUCTION_BOT_ID_FORBIDDEN")
    try:
        owner_id = int(values.get("OWNER_TELEGRAM_ID") or "")
        if owner_id <= 0:
            raise ValueError()
    except ValueError:
        raise SetupError("TEST_OWNER_REQUIRED") from None
    # Explicit defaults prevent any ambient production env from being inherited.
    defaults = {field.alias: field.default for field in Settings.model_fields.values() if not field.is_required()}
    defaults.update(BOT_TOKEN=token, OWNER_TELEGRAM_ID=owner_id, BOT_MODE="polling",
        DATABASE_URL="sqlite+aiosqlite:////data/planner.db", WEB_HOST="0.0.0.0", WEB_PORT=8000,
        BOOKING_RUNTIME_ENABLED=True, CALENDAR_ENVIRONMENT="meetly-test")
    settings = Settings(_env_file=None, **defaults)
    lines = []
    for name, field in Settings.model_fields.items():
        value = getattr(settings, name)
        value = str(value).lower() if isinstance(value, bool) else str(value)
        if "\n" in value or "\r" in value:
            raise SetupError("UNSAFE_ENV_VALUE")
        lines.append(f"{field.alias}={value}")
    private_root = root/".deploy"
    destination = Path(destination) if destination is not None else private_root/"meetly-test"
    if not destination.is_absolute():
        destination = root/destination
    resolved = destination.resolve()
    if (private_root.is_symlink() or not private_root.resolve().is_relative_to(root)
            or resolved == private_root.resolve() or not resolved.is_relative_to(private_root.resolve())
            or destination.exists() or destination.is_symlink()):
        raise SetupError("NEW_PRIVATE_DIRECTORY_REQUIRED")
    destination.mkdir(parents=True, mode=0o777 if os.name == "nt" else 0o700)
    # No tokens are returned or printed. O_EXCL prevents overwriting a secret file.
    with os.fdopen(os.open(destination/"app.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as target:
        target.write("\n".join(lines)+"\n")
    (destination/"compose.env").write_text(f"MEETLY_TEST_IMAGE={image}\nMEETLY_TEST_PORT={host_port}\n", encoding="utf-8")
    shutil.copyfile(Path(__file__).parent/"meetly-test"/"compose.yml", destination/"compose.yml")
    (destination/"data").mkdir()
    return dict(state="prepared", directory=str(destination), image=image, host_port=host_port,
                environment="meetly-test", google_enabled=False, smtp_enabled=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--port", type=int, default=8013)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    try:
        result = prepare(image=args.image, host_port=args.port, destination=args.destination)
    except SetupError as error:
        print(json.dumps(dict(state="not_prepared", error_code=str(error))))
        return 1
    except Exception:
        print(json.dumps(dict(state="not_prepared", error_code="TEST_SETUP_FAILED")))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
