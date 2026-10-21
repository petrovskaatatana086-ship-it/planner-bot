"""Store consent version, preserving existing consent_at and profiles.

Revision ID: 0010_consent_version
Revises: 0009_reschedule_history
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_consent_version"
down_revision = "0009_reschedule_history"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("consent_version", sa.String(64), nullable=True))


def downgrade():
    raise RuntimeError("Consent evidence must be retained; use a verified restore")
