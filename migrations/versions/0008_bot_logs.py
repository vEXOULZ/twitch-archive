"""Chat from doomtp-bot's /log API: bot_logs, vod_splice_bot_logs, vods.bot_chat (additive).

The replay crawl keeps filling ``logs``; the bot's chat goes into its own table so
both are kept. ``vods.bot_chat`` is NULL for every existing row and nothing that
reads ``vods`` needs it, so the previous release keeps working.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-30
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vods", sa.Column("bot_chat", JSONB))

    op.execute("CREATE SEQUENCE bot_logs_seq_seq")
    op.create_table(
        "bot_logs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("seq", sa.BigInteger, nullable=False, server_default=sa.text("nextval('bot_logs_seq_seq')")),
        sa.Column("vod_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_offset_seconds", sa.Integer, nullable=False),
        sa.Column("user_id", sa.Text),
        sa.Column("user_login", sa.Text),
        sa.Column("display_name", sa.Text),
        sa.Column("message", JSONB, nullable=False),
        sa.Column("user_badges", JSONB, nullable=False),
        sa.Column("user_color", sa.Text, nullable=False),
        sa.Column("message_type", sa.Text),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("cleared_at", sa.DateTime(timezone=True)),
        sa.Column("data", JSONB, nullable=False),
        sa.Column("createdAt", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updatedAt", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute("ALTER SEQUENCE bot_logs_seq_seq OWNED BY bot_logs.seq")
    op.create_index("bot_logs_vod_offset_idx", "bot_logs", ["vod_id", "content_offset_seconds", "seq"])

    op.create_table(
        "vod_splice_bot_logs",
        sa.Column("splice_id", sa.BigInteger, sa.ForeignKey("vod_splices.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("bot_log_id", sa.Text, primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("vod_splice_bot_logs")
    op.drop_table("bot_logs")  # drops the owned sequence too
    op.drop_column("vods", "bot_chat")
