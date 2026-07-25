"""meeting reschedule fields

Revision ID: 0004_meeting_reschedule_fields
Revises: 0003_oauth_state
Create Date: 2026-07-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_meeting_reschedule_fields"
down_revision: Union[str, None] = "0003_oauth_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("meetings", sa.Column("proposed_start_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("meetings", sa.Column("proposed_end_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("meetings", sa.Column("reschedule_comment", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("meetings", "reschedule_comment")
    op.drop_column("meetings", "proposed_end_at")
    op.drop_column("meetings", "proposed_start_at")

