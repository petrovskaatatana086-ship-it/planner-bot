"""Real Alembic revision operations against synthetic legacy SQLite, no .env."""
import importlib.util
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from app.db.base import Base
from app.db import models  # registers metadata

ROOT = Path(__file__).resolve().parents[1]


def revision(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "migrations" / "versions" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BookingMigrationTest(unittest.TestCase):
    def setUp(self):
        self.path = Path.cwd() / f".test_migration_{uuid.uuid4().hex}.db"
        self.engine = sa.create_engine(f"sqlite:///{self.path.as_posix()}")
        self.connection = self.engine.connect()
        self.ops = Operations(MigrationContext.configure(self.connection))
        for name in ("0001_initial", "0002_google_oauth_credentials", "0003_oauth_state",
                     "0004_meeting_reschedule_fields", "0005_meeting_google_event_url"):
            module = revision(name)
            with patch.object(module, "op", self.ops):
                module.upgrade()
        self.connection.execute(sa.text(
            "INSERT INTO users(id,telegram_id,full_name,role,status,timezone,created_at,updated_at,last_activity_at) "
            "VALUES(1,101,'Synthetic','client','active','Europe/Moscow',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        self.connection.execute(sa.text(
            "INSERT INTO booking_requests(id,client_id,title,start_at,end_at,timezone,status,created_at,updated_at) "
            "VALUES(1,1,'Synthetic','2026-10-26 07:00:00','2026-10-26 08:00:00','Europe/Moscow','approved',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        self.insert_meeting(1)
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        self.engine.dispose()
        self.path.unlink(missing_ok=True)

    def insert_meeting(self, identifier):
        self.connection.execute(sa.text(
            "INSERT INTO meetings(id,booking_request_id,client_id,title,start_at,end_at,timezone,status,created_at,updated_at) "
            "VALUES(:id,1,1,'Synthetic','2026-10-26 07:00:00','2026-10-26 08:00:00','Europe/Moscow','confirmed',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
            {"id": identifier})

    def upgrade(self):
        module = revision("0006_booking_commands")
        with patch.object(module, "op", self.ops):
            module.upgrade()
        lease_revision = revision("0007_outbox_lease_token")
        with patch.object(lease_revision, "op", self.ops):
            lease_revision.upgrade()
        calendar_revision = revision("0008_calendar_sync")
        with patch.object(calendar_revision, "op", self.ops):
            calendar_revision.upgrade()
        history_revision = revision("0009_reschedule_history")
        with patch.object(history_revision, "op", self.ops):
            history_revision.upgrade()
        consent_revision = revision("0010_consent_version")
        with patch.object(consent_revision, "op", self.ops):
            consent_revision.upgrade()
        self.connection.commit()

    def test_upgrade_preserves_legacy_records_and_enforces_uniqueness(self):
        self.upgrade()
        self.assertEqual(self.connection.scalar(sa.text("SELECT version FROM meetings WHERE id=1")), 1)
        self.assertEqual(self.connection.scalar(sa.text("SELECT title FROM booking_requests WHERE id=1")), "Synthetic")
        with self.assertRaises(IntegrityError):
            self.insert_meeting(2)
        self.connection.rollback()
        self.assertIn("operation_results", sa.inspect(self.connection).get_table_names())
        self.assertIn("outbox", sa.inspect(self.connection).get_table_names())
        # Ensure model metadata and revision produce the same field set.
        for name in ("users", "meetings", "booking_requests", "owner_profiles", "operation_results", "outbox", "calendar_sync", "reschedule_requests"):
            actual = {c["name"] for c in sa.inspect(self.connection).get_columns(name)}
            self.assertEqual(actual, set(Base.metadata.tables[name].columns.keys()))

    def test_duplicate_meetings_stop_before_schema_changes(self):
        self.insert_meeting(2)
        self.connection.commit()
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            self.upgrade()
        self.assertNotIn("version", {c["name"] for c in sa.inspect(self.connection).get_columns("meetings")})
        self.assertEqual(self.connection.scalar(sa.text("SELECT count(*) FROM meetings")), 2)

    def test_unknown_status_stops_before_schema_changes(self):
        self.connection.execute(sa.text("UPDATE meetings SET status='rescheduled_pending'"))
        self.connection.commit()
        with self.assertRaisesRegex(RuntimeError, "invalid booking"):
            self.upgrade()
        self.assertNotIn("outbox", sa.inspect(self.connection).get_table_names())

    def test_orphan_stops_before_schema_changes(self):
        self.connection.execute(sa.text("UPDATE meetings SET client_id=999"))
        self.connection.commit()
        with self.assertRaisesRegex(RuntimeError, "invalid booking"):
            self.upgrade()

    def test_downgrade_refuses_to_destroy_durable_commands(self):
        self.upgrade()
        module = revision("0006_booking_commands")
        with patch.object(module, "op", self.ops), self.assertRaisesRegex(RuntimeError, "backup"):
            module.downgrade()

    def test_partial_reschedule_stops_before_schema_changes(self):
        self.connection.execute(sa.text("UPDATE meetings SET status='reschedule_pending'"))
        self.connection.commit()
        with self.assertRaisesRegex(RuntimeError, "invalid booking"):
            self.upgrade()

    def test_valid_legacy_proposal_is_preserved_without_guessing_previous_status(self):
        self.connection.execute(sa.text("UPDATE meetings SET status='reschedule_pending', "
            "proposed_start_at='2026-10-27 07:00:00', proposed_end_at='2026-10-27 08:00:00', "
            "reschedule_comment='Synthetic legacy proposal'"))
        self.connection.commit()
        self.upgrade()
        proposal = self.connection.execute(sa.text(
            "SELECT previous_status,status,comment FROM reschedule_requests WHERE meeting_id=1")).first()
        self.assertEqual(tuple(proposal), (None, "pending", "Synthetic legacy proposal"))
        self.assertEqual(self.connection.scalar(sa.text("SELECT status FROM meetings WHERE id=1")), "reschedule_pending")

    def test_legacy_consent_and_name_survive_nullable_consent_version_migration(self):
        self.connection.execute(sa.text("UPDATE users SET email='legacy@example.com', "
            "full_name='Legacy entered name', consent_at='2026-10-01 07:00:00' WHERE id=1"))
        self.connection.commit()
        self.upgrade()
        row = self.connection.execute(sa.text("SELECT full_name,email,consent_at,consent_version FROM users WHERE id=1")).first()
        self.assertEqual(tuple(row), ("Legacy entered name", "legacy@example.com", "2026-10-01 07:00:00", None))
