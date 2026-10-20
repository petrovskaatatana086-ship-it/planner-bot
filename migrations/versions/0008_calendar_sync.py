"""Durable desired/applied Google Calendar state.

Revision ID: 0008_calendar_sync
Revises: 0007_outbox_lease_token
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_calendar_sync"
down_revision = "0007_outbox_lease_token"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "calendar_sync",
        sa.Column("meeting_id", sa.Integer(), sa.ForeignKey("meetings.id"), primary_key=True),
        sa.Column("environment", sa.String(64), nullable=False),
        sa.Column("calendar_id", sa.String(512), nullable=False),
        sa.Column("deterministic_event_id", sa.String(255), nullable=False),
        sa.Column("conference_request_id", sa.String(32), nullable=False),
        sa.Column("desired_version", sa.Integer(), nullable=False),
        sa.Column("applied_version", sa.Integer(), nullable=False),
        sa.Column("applied_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("last_error_code", sa.String(64), nullable=True),
    )


def downgrade():
    raise RuntimeError("Calendar sync state requires a verified restore, not destructive downgrade")
