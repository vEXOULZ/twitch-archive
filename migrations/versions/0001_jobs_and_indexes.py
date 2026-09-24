"""Worker job tables and chat indexes (additive; the legacy app keeps working).

Revision ID: 0001
Revises: 0000
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = "0000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("vod_id", sa.Text, index=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="queued"),
        sa.Column("step", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("jobs_state_idx", "jobs", ["state", "id"])

    op.create_table(
        "app_state",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # The chat endpoint filters by vod_id and orders by (content_offset_seconds, _id).
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS logs_vod_offset_idx "
            "ON logs (vod_id, content_offset_seconds, _id)"
        )
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS logs_vod_seq_idx ON logs (vod_id, _id)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS logs_vod_seq_idx")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS logs_vod_offset_idx")
    op.drop_table("app_state")
    op.drop_index("jobs_state_idx", table_name="jobs")
    op.drop_table("jobs")
