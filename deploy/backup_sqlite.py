"""Consistent SQLite snapshots; only rotate this project's dated snapshots."""

import argparse
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    snapshot = args.destination / f"calendar-{stamp}.sqlite3"
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(snapshot) as target:
            source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError("Backup integrity check failed")
    snapshots = sorted(args.destination.glob("calendar-????????T????????????Z.sqlite3"))
    for expired in snapshots[:-14]:
        expired.unlink()
    print(f"SQLite backup verified: {snapshot.name}")


if __name__ == "__main__":
    main()
