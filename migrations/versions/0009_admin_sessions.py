"""Admin dashboard sessions in the database, and who signed in on each audit row (additive).

Sessions used to live in the worker's memory, so every restart signed the dashboard out.
``admin_sessions`` keeps them (by token hash; the token itself is only ever in the cookie).
``admin_audit.actor_login`` is the Twitch login behind a ``twitch:<id>`` actor, NULL for the
password and the API key and for every existing row.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-30
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_sessions",
        sa.Column("token_hash", sa.Text, primary_key=True),
        sa.Column("csrf", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("twitch_user", JSONB),
        sa.Column("sid", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("admin_sessions_expires_at", "admin_sessions", ["expires_at"])
    op.add_column("admin_audit", sa.Column("actor_login", sa.Text))


def downgrade() -> None:
    op.drop_column("admin_audit", "actor_login")
    op.drop_index("admin_sessions_expires_at", table_name="admin_sessions")
    op.drop_table("admin_sessions")
