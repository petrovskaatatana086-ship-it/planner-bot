"""add google_event_url to meetings

Revision ID: 0005_meeting_google_event_url
Revises: 0004_meeting_reschedule_fields
Create Date: 2026-08-03 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_meeting_google_event_url"
down_revision = "0004_meeting_reschedule_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("meetings", sa.Column("google_event_url", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column("meetings", "google_event_url")
