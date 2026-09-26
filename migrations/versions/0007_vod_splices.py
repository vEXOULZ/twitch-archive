"""Merging and splitting VODs: vods.merged_into, vod_splices, vod_splice_logs (additive).

``vods.merged_into`` is NULL for every existing row, and nothing that reads
``vods`` needs to know about it, so the previous release keeps working.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vods", sa.Column("merged_into", JSONB))

    op.create_table(
        "vod_splices",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("vod_id", sa.Text, nullable=False),
        sa.Column("other_id", sa.Text, nullable=False),
        sa.Column("offset_s", sa.Numeric, nullable=False),
        sa.Column("detail", JSONB, nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("undone_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_vod_splices_vod_id", "vod_splices", ["vod_id"])
    op.create_index("ix_vod_splices_other_id", "vod_splices", ["other_id"])

    op.create_table(
        "vod_splice_logs",
        sa.Column("splice_id", sa.BigInteger, sa.ForeignKey("vod_splices.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("log_id", UUID(as_uuid=True), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("vod_splice_logs")
    op.drop_index("ix_vod_splices_other_id", table_name="vod_splices")
    op.drop_index("ix_vod_splices_vod_id", table_name="vod_splices")
    op.drop_table("vod_splices")
    op.drop_column("vods", "merged_into")
