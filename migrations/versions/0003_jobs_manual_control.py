"""jobs.pause_before / jobs.pause_next: manual step gates and single-stepping.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("pause_before", ARRAY(sa.Text)))
    op.add_column("jobs", sa.Column("pause_next", sa.Boolean, nullable=False, server_default=sa.false()))


def downgrade() -> None:
    # Paused jobs have no meaning without the columns; hand them back to the queue.
    op.execute("UPDATE jobs SET state = 'queued' WHERE state = 'paused'")
    op.drop_column("jobs", "pause_next")
    op.drop_column("jobs", "pause_before")
