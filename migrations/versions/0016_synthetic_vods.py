"""Synthetic VODs: vods.synthetic, vods.tags, vod_segments (additive).

A synthetic VOD (a merge, a split, a playthrough) is a ``vods`` row whose content is a list of
``vod_segments``: windows of real VODs placed on its own timeline. The real VODs are never touched.
``vods.synthetic`` is NULL on every real VOD, otherwise ``{"supersedes": bool}`` (the originals leave
the public lists and redirect into it). ``vods.tags`` is ``{}`` on every existing row: an untagged VOD
is listed as a regular one, a tagged one (``compilation``) gets its own tab. Nothing that reads
``vods`` today needs either column, so the previous release keeps working.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vods", sa.Column("synthetic", JSONB))
    op.add_column("vods", sa.Column("tags", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")))
    op.create_index("ix_vods_tags", "vods", ["tags"], postgresql_using="gin")

    op.create_table(
        "vod_segments",
        sa.Column(
            "vod_id", sa.Text, sa.ForeignKey("vods.id", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("pos", sa.Integer, primary_key=True),
        sa.Column(
            "source_id", sa.Text, sa.ForeignKey("vods.id", onupdate="CASCADE", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("start_s", sa.Numeric, nullable=False),
        sa.Column("end_s", sa.Numeric),  # NULL: to the source's end, however long it gets
        sa.Column("at_s", sa.Numeric, nullable=False),
        sa.Column("label", sa.Text),
    )
    op.create_index("ix_vod_segments_source_id", "vod_segments", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_vod_segments_source_id", table_name="vod_segments")
    op.drop_table("vod_segments")
    op.drop_index("ix_vods_tags", table_name="vods")
    op.drop_column("vods", "tags")
    op.drop_column("vods", "synthetic")
