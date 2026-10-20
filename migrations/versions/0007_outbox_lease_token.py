"""Fence outbox claims so an expired worker cannot acknowledge a newer lease.

Revision ID: 0007_outbox_lease_token
Revises: 0006_booking_commands
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_outbox_lease_token"
down_revision = "0006_booking_commands"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("outbox", sa.Column("lease_token", sa.String(32), nullable=True))


def downgrade():
    raise RuntimeError("Outbox fencing cannot be removed while workers or deliveries may exist")
