"""Preserve bot command versions across Telegram retries and process restarts."""
from alembic import op
import sqlalchemy as sa

revision = "0011_bot_command_inputs"
down_revision = "0010_consent_version"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("bot_command_inputs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("command_id", sa.String(128), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("actor_id", "command_id", name="uq_bot_input_actor_command"))


def downgrade():
    raise RuntimeError("Bot replay evidence must be retained; use a verified restore")
