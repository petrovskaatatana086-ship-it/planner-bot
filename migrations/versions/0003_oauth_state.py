"""oauth state

Revision ID: 0003_oauth_state
Revises: 0002_google_oauth_credentials
Create Date: 2026-07-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_oauth_state"
down_revision: Union[str, None] = "0002_google_oauth_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("oauth_credentials", sa.Column("oauth_state", sa.String(length=255), nullable=True))
    op.create_index("ix_oauth_credentials_oauth_state", "oauth_credentials", ["oauth_state"])


def downgrade() -> None:
    op.drop_index("ix_oauth_credentials_oauth_state", table_name="oauth_credentials")
    op.drop_column("oauth_credentials", "oauth_state")

