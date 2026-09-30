"""Worker settings changed from the admin dashboard (additive).

``settings``: one row per overridden setting (see archive_worker/runtime_settings.py for which
can be). A row wins over the environment variable; deleting it goes back to the env value.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-30
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", sa.Text),
    )


def downgrade() -> None:
    op.drop_table("settings")
