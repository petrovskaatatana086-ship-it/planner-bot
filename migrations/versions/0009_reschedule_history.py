"""Structured reschedule history, preserve legacy proposals without guessing status.

Revision ID: 0009_reschedule_history
Revises: 0008_calendar_sync
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_reschedule_history"
down_revision = "0008_calendar_sync"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    malformed = connection.execute(sa.text(
        "SELECT id FROM meetings WHERE status='reschedule_pending' AND "
        "(proposed_start_at IS NULL OR proposed_end_at IS NULL OR proposed_end_at <= proposed_start_at) LIMIT 1"
    )).first()
    if malformed:
        raise RuntimeError("Migration preflight: incomplete legacy reschedule")
    op.add_column("meetings", sa.Column("cancellation_reason", sa.Text(), nullable=True))
    op.create_table("reschedule_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("meeting_id", sa.Integer(), sa.ForeignKey("meetings.id"), nullable=False),
        sa.Column("requested_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("proposed_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proposed_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("previous_status", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_reschedule_requests_meeting_id", "reschedule_requests", ["meeting_id"])
    op.create_index("uq_reschedule_pending_meeting", "reschedule_requests", ["meeting_id"], unique=True,
                    sqlite_where=sa.text("status = 'pending'"), postgresql_where=sa.text("status = 'pending'"))
    connection.execute(sa.text(
        "INSERT INTO reschedule_requests(meeting_id,requested_by_id,proposed_start_at,proposed_end_at,"
        "comment,previous_status,status,created_at) SELECT id,client_id,proposed_start_at,proposed_end_at,"
        "reschedule_comment,NULL,'pending',updated_at FROM meetings WHERE status='reschedule_pending'"))


def downgrade():
    raise RuntimeError("Reschedule history must be retained; use a verified backup to restore")
