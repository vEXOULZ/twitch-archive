"""emotes.global_emotes: the 7TV/BTTV/FFZ global sets saved with each VOD (additive).

NULL means "never saved"; the ``global_emotes_backfill`` job fills those rows.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("emotes", sa.Column("global_emotes", JSONB))
    op.add_column("emotes", sa.Column("global_emotes_source", sa.Text))  # 'captured' | 'backfilled'
    op.add_column("emotes", sa.Column("global_emotes_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("emotes", "global_emotes_at")
    op.drop_column("emotes", "global_emotes_source")
    op.drop_column("emotes", "global_emotes")
