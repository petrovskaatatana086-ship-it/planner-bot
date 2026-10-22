"""Revocable Mini App sessions; never persist bearer tokens or Telegram initData."""
from alembic import op
import sqlalchemy as sa

revision = "0012_api_sessions"
down_revision = "0011_bot_command_inputs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("api_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_api_sessions_user_id", "api_sessions", ["user_id"])
    op.create_index("ix_api_sessions_expires_at", "api_sessions", ["expires_at"])


def downgrade():
    raise RuntimeError("Use a verified backup/restore rather than destructive downgrade")
