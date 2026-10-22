"""Real Alembic upgrade and SQLite backup/restore on synthetic legacy databases."""
import json
from contextlib import closing, contextmanager
from pathlib import Path
import shutil
import sqlite3
import unittest
import uuid
from unittest.mock import patch

from alembic import command
from alembic.config import Config
import sqlalchemy as sa

from deploy.rehearse_migration import fingerprint, rehearse, revision, upgrade

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as db:
        with db:
            yield db


class MigrationRehearsalTest(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd()/f".test_rehearsal_{uuid.uuid4().hex}"
        self.root.mkdir()
        self.source = self.root/"legacy.db"
        config = Config()
        config.set_main_option("script_location", str(ROOT/"migrations"))
        engine = sa.create_engine(sa.URL.create("sqlite", database=str(self.source)))
        try:
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "0005_meeting_google_event_url")
                connection.commit()
        finally:
            engine.dispose()
        with database(self.source) as db:
            db.execute("INSERT INTO users(id,telegram_id,full_name,email,role,status,timezone,consent_at,created_at,updated_at,last_activity_at) "
                "VALUES(1,101,'Synthetic','synthetic@example.com','client','active','Europe/Moscow',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
            db.execute("INSERT INTO booking_requests(id,client_id,title,start_at,end_at,timezone,status,created_at,updated_at) "
                "VALUES(1,1,'Synthetic','2026-10-26 07:00:00','2026-10-26 08:00:00','Europe/Moscow','approved',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
            db.execute("INSERT INTO meetings(id,booking_request_id,client_id,title,start_at,end_at,timezone,status,google_event_id,created_at,updated_at) "
                "VALUES(1,1,1,'Synthetic','2026-10-26 07:00:00','2026-10-26 08:00:00','Europe/Moscow','confirmed','legacy-event',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")

    def tearDown(self):
        # Only this test's resolved, unique directory is removed.
        target = self.root.resolve()
        self.assertEqual(target.parent, Path.cwd().resolve())
        self.assertTrue(target.name.startswith(".test_rehearsal_"))
        shutil.rmtree(target)

    def run_rehearsal(self):
        return rehearse(self.source, self.root/"rehearsal")

    def test_real_upgrade_restore_and_no_settings_or_secret_output(self):
        before = fingerprint(self.source)
        with patch("app.config.get_settings", side_effect=AssertionError("No .env allowed")):
            report = self.run_rehearsal()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["original_revision"], ["0005_meeting_google_event_url"])
        self.assertEqual(report["upgraded_revision"], ["0012_api_sessions"])
        self.assertEqual(fingerprint(self.source), before)
        self.assertEqual(fingerprint(self.root/"rehearsal"/"restored.sqlite3"), before)
        serialized = json.dumps(report)
        for value in ("Synthetic", "synthetic@example.com", "legacy-event"):
            self.assertNotIn(value, serialized)

    def test_duplicate_data_stops_upgrade_and_restore_still_matches(self):
        with database(self.source) as db:
            db.execute("INSERT INTO meetings(booking_request_id,client_id,title,start_at,end_at,timezone,status,created_at,updated_at) "
                "SELECT booking_request_id,client_id,title,start_at,end_at,timezone,status,created_at,updated_at FROM meetings")
        report = self.run_rehearsal()
        self.assertFalse(report["ok"])
        self.assertFalse(report["migration_ok"])
        self.assertTrue(report["restore_verified"])
        self.assertTrue(report["source_unchanged"])
        self.assertEqual(report["error_code"], "MIGRATION_REHEARSAL_FAILED")
        self.assertEqual(revision(self.root/"rehearsal"/"restored.sqlite3"), ["0005_meeting_google_event_url"])

    def test_partial_upgrade_failure_never_contaminates_restore(self):
        def broken(path):
            with database(path) as db:
                db.execute("CREATE TABLE partial_upgrade (id INTEGER)")
            raise RuntimeError("SENSITIVE DATABASE VALUE")
        with patch("deploy.rehearse_migration.upgrade", side_effect=broken):
            report = self.run_rehearsal()
        self.assertFalse(report["ok"])
        self.assertTrue(report["restore_verified"])
        self.assertNotIn("SENSITIVE", json.dumps(report))
        self.assertNotEqual(fingerprint(self.root/"rehearsal"/"upgraded.sqlite3"), fingerprint(self.source))

    def test_wal_backup_includes_committed_rows_and_excludes_uncommitted_rows(self):
        with database(self.source) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("UPDATE users SET full_name='Committed WAL' WHERE id=1")
            db.commit()
            db.execute("UPDATE users SET full_name='Uncommitted WAL' WHERE id=1")
            report = self.run_rehearsal()
            self.assertTrue(report["ok"], report)
            with database(self.root/"rehearsal"/"restored.sqlite3") as restored:
                self.assertEqual(restored.execute("SELECT full_name FROM users").fetchone()[0], "Committed WAL")
            db.rollback()

    def test_source_changed_during_upgrade_is_reported(self):
        def change_source(path):
            upgrade(path)
            with database(self.source) as db:
                db.execute("UPDATE users SET full_name='Concurrent writer' WHERE id=1")
        with patch("deploy.rehearse_migration.upgrade", side_effect=change_source):
            report = self.run_rehearsal()
        self.assertFalse(report["ok"])
        self.assertFalse(report["source_unchanged"])
        self.assertTrue(report["restore_verified"])

    def test_existing_directory_and_missing_source_cannot_overwrite_files(self):
        workdir = self.root/"exists"
        workdir.mkdir()
        marker = workdir/"marker"
        marker.write_text("Keep")
        with self.assertRaises(ValueError):
            rehearse(self.source, workdir)
        self.assertEqual(marker.read_text(), "Keep")
        with self.assertRaises(FileNotFoundError):
            rehearse(self.root/"missing.db", self.root/"new")
        self.assertFalse((self.root/"new").exists())

    def test_legacy_proposal_keeps_unknown_previous_status_and_all_source_columns(self):
        with database(self.source) as db:
            db.execute("UPDATE meetings SET status='reschedule_pending', proposed_start_at='2026-10-27 07:00:00', "
                       "proposed_end_at='2026-10-27 08:00:00', reschedule_comment='Synthetic proposal'")
        report = self.run_rehearsal()
        self.assertTrue(report["ok"], report)
        with database(self.root/"rehearsal"/"upgraded.sqlite3") as db:
            self.assertEqual(db.execute("SELECT previous_status,status FROM reschedule_requests").fetchone(), (None, "pending"))

    def test_unversioned_database_is_never_stamped_implicitly(self):
        with database(self.source) as db:
            db.execute("DROP TABLE alembic_version")
        report = self.run_rehearsal()
        self.assertFalse(report["ok"])
        self.assertTrue(report["restore_verified"])
        self.assertTrue(report["source_unchanged"])
