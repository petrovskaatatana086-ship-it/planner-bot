"""Offline SQLite upgrade/restore rehearsal. Source is opened read-only; no .env/network."""
import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import sqlalchemy as sa

from app.db.base import Base
from app.db import models  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def quote(value):
    return '"' + value.replace('"', '""') + '"'


@contextlib.contextmanager
def readonly(path):
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        yield db


def verify(db):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise RuntimeError("SQLITE_INTEGRITY_FAILED")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("SQLITE_FOREIGN_KEYS_FAILED")


def backup(source, target):
    # Exclusive creation prevents accidental overwrite, including symlink targets.
    with target.open("xb"):
        pass
    with readonly(source) as src, contextlib.closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        verify(dst)


def fingerprint(path, projection=None):
    with readonly(path) as db:
        verify(db)
        schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        tables = projection or {row[1]: [column[1] for column in db.execute(
            f"PRAGMA table_info({quote(row[1])})")] for row in schema if row[0] == "table"}
        result = {}
        for table, columns in tables.items():
            fields = ",".join(map(quote, columns))
            hasher, count = hashlib.sha256(), 0
            for row in db.execute(f"SELECT {fields} FROM {quote(table)} ORDER BY {fields}"):
                values = [dict(blob=base64.b64encode(value).decode()) if isinstance(value, bytes) else value for value in row]
                hasher.update(json.dumps(values, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()+b"\n")
                count += 1
            result[table] = dict(columns=columns, count=count, sha256=hasher.hexdigest())
        return dict(schema_sha256=hashlib.sha256(json.dumps(schema).encode()).hexdigest(), tables=result)


def revision(path):
    with readonly(path) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='alembic_version'").fetchone() is None:
            raise RuntimeError("ALEMBIC_REVISION_MISSING")
        return [row[0] for row in db.execute("SELECT version_num FROM alembic_version ORDER BY version_num")]


def upgrade(path):
    config = Config()
    config.set_main_option("script_location", str(ROOT/"migrations"))
    engine = sa.create_engine(sa.URL.create("sqlite", database=str(path.resolve())))
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
            inspector = sa.inspect(connection)
            for name, table in Base.metadata.tables.items():
                if name not in inspector.get_table_names() or not set(table.columns.keys()).issubset(
                        {column["name"] for column in inspector.get_columns(name)}):
                    raise RuntimeError("UPGRADED_SCHEMA_INCOMPLETE")
        if revision(path) != sorted(ScriptDirectory.from_config(config).get_heads()):
            raise RuntimeError("UPGRADED_REVISION_INCORRECT")
    finally:
        engine.dispose()


def rehearse(source, workdir):
    source, workdir = Path(source).resolve(strict=True), Path(workdir).absolute()
    if not source.is_file() or workdir.exists() or workdir.is_symlink():
        raise ValueError("A source file and a new rehearsal directory are required")
    # Windows 0o700 installs a special DACL that excludes sandbox principals.
    # Inherit the workspace ACL there; use a private directory on POSIX.
    workdir.mkdir(mode=0o777 if os.name == "nt" else 0o700, parents=True, exist_ok=False)
    snapshot, migrated, restored = [workdir/name for name in ("before.sqlite3", "upgraded.sqlite3", "restored.sqlite3")]
    report = dict(ok=False, migration_ok=False, legacy_data_preserved=False, restore_verified=False,
                  source_unchanged=False)
    try:
        backup(source, snapshot)
        before = fingerprint(snapshot)
        backup(snapshot, migrated)
        try:
            report["original_revision"] = revision(snapshot)
            upgrade(migrated)
            report["upgraded_revision"] = revision(migrated)
            projection = {name: data["columns"] for name, data in before["tables"].items()
                          if name != "alembic_version"}
            after = fingerprint(migrated, projection)["tables"]
            report["legacy_data_preserved"] = all(after[name] == before["tables"][name] for name in projection)
            report["migration_ok"] = True
        except Exception:
            # Never serialize database contents/SQL parameters from a migration exception.
            report["error_code"] = "MIGRATION_REHEARSAL_FAILED"
        finally:
            backup(snapshot, restored)
            report["restore_verified"] = fingerprint(restored) == before
            report["source_unchanged"] = fingerprint(source) == before
        report["row_counts"] = {name: data["count"] for name, data in before["tables"].items()}
        report["ok"] = all(report[key] for key in ("migration_ok", "legacy_data_preserved", "restore_verified", "source_unchanged"))
    except Exception:
        report["error_code"] = "SNAPSHOT_OR_VERIFICATION_FAILED"
    (workdir/"report.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True, help="New private directory for snapshots and report")
    args = parser.parse_args()
    try:
        report = rehearse(args.database, args.workdir)
    except Exception:
        print(json.dumps(dict(ok=False, error_code="INVALID_REHEARSAL_PATH")))
        return 1
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
