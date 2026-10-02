"""How the site shows VOD tags (additive): site_settings, site_tag_shapes.

``site_settings``: one row per setting of vods.vexoulz.net that its admin edits here; ``tags`` holds
the tag list in display order (see archive_common/site_tags.py). No row: never saved, and the site
uses its built-in defaults. ``site_tag_shapes``: each tag's uploaded SVG, cleaned
(archive_worker/svg_clean.py), served by archive-api at ``/v1/site/tags/{name}.svg``.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_settings",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", sa.Text),
    )
    op.create_table(
        "site_tag_shapes",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("svg", sa.Text, nullable=False),
        sa.Column("hash", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("site_tag_shapes")
    op.drop_table("site_settings")
