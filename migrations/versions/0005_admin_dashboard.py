"""Admin dashboard: vods.chapters_locked, job_events, admin_audit (additive).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vods", sa.Column("chapters_locked", sa.Boolean, nullable=False, server_default=sa.false()))

    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("level", sa.Text, nullable=False),
        sa.Column("step", sa.Text),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("progress", JSONB),
    )
    op.create_index("job_events_job_idx", "job_events", ["job_id", "id"])

    op.create_table(
        "admin_audit",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target", sa.Text),
        sa.Column("detail", JSONB),
    )


def downgrade() -> None:
    op.drop_table("admin_audit")
    op.drop_index("job_events_job_idx", table_name="job_events")
    op.drop_table("job_events")
    op.drop_column("vods", "chapters_locked")
