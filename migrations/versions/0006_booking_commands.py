"""Add versions, durable command results and delivery records.

Revision ID: 0006_booking_commands
Revises: 0005_meeting_google_event_url
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_booking_commands"
down_revision = "0005_meeting_google_event_url"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    duplicates = connection.execute(sa.text(
        "SELECT booking_request_id FROM meetings GROUP BY booking_request_id HAVING count(*) > 1 LIMIT 1"
    )).first()
    if duplicates:
        raise RuntimeError("Migration preflight: duplicate meetings for a booking request")
    invalid = connection.execute(sa.text(
        "SELECT id FROM meetings WHERE end_at <= start_at OR "
        "status NOT IN ('confirmed','rescheduled','reschedule_pending','cancelled_by_owner','cancelled_by_client') LIMIT 1"
    )).first()
    invalid_request = connection.execute(sa.text(
        "SELECT id FROM booking_requests WHERE end_at <= start_at OR "
        "status NOT IN ('pending','approved','rejected','withdrawn') LIMIT 1"
    )).first()
    orphan = connection.execute(sa.text(
        "SELECT m.id FROM meetings m LEFT JOIN booking_requests b ON m.booking_request_id=b.id "
        "LEFT JOIN users u ON m.client_id=u.id WHERE b.id IS NULL OR u.id IS NULL "
        "OR b.client_id != m.client_id LIMIT 1"
    )).first()
    orphan_request = connection.execute(sa.text(
        "SELECT b.id FROM booking_requests b LEFT JOIN users u ON b.client_id=u.id WHERE u.id IS NULL LIMIT 1"
    )).first()
    malformed_proposal = connection.execute(sa.text(
        "SELECT id FROM meetings WHERE "
        "(status = 'reschedule_pending' AND (proposed_start_at IS NULL OR proposed_end_at IS NULL "
        "OR proposed_end_at <= proposed_start_at)) OR "
        "(status != 'reschedule_pending' AND (proposed_start_at IS NOT NULL OR proposed_end_at IS NOT NULL)) LIMIT 1"
    )).first()
    foreign_key_error = (
        connection.execute(sa.text("PRAGMA foreign_key_check")).first()
        if connection.dialect.name == "sqlite" else None
    )
    if invalid or invalid_request or orphan or orphan_request or malformed_proposal or foreign_key_error:
        raise RuntimeError("Migration preflight: invalid booking data; repair before upgrading")
    for table in ("owner_profiles", "booking_requests", "meetings"):
        op.add_column(table, sa.Column("version", sa.Integer(), server_default="1", nullable=False))
    # SQLite supports a unique index without rebuilding the meetings table.
    op.drop_index("ix_meetings_booking_request_id", table_name="meetings")
    op.create_index("ix_meetings_booking_request_id", "meetings", ["booking_request_id"], unique=True)
    op.create_table(
        "operation_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("command_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("actor_id", "command_id", name="uq_operation_actor_command"),
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", "channel", "kind", name="uq_outbox_delivery"),
    )
    op.create_index("ix_outbox_state", "outbox", ["state"])
    op.create_index("ix_outbox_next_attempt_at", "outbox", ["next_attempt_at"])


def downgrade():
    # Never silently discard accepted commands or pending external deliveries.
    raise RuntimeError("Restore a verified backup with stopped writers; automatic downgrade is disabled")
