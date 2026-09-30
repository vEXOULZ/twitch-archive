"""Hidden VODs (additive).

``vods.hidden``: set from the admin dashboard to take a VOD off the public API (lists,
``GET /vods/{id}``, its games rows, chat, games-played and status) without deleting it.
The ``vods_changed`` trigger from 0006 already fires on the update, so archive-api drops
its cached copies.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vods", sa.Column("hidden", sa.Boolean, nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("vods", "hidden")
