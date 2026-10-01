"""vex-platform 0.6.0's ``job_runs.parent_id`` column (additive).

vex-platform 0.6.0 records which run queued a run (a step's ``enqueue``), for GET /jobs/{id}/related. It
reads the column, so this runs before that version starts; an image still on 0.5.0 ignores it. The
bot-chat backfill is the first job here to queue others: one ``bot_chat`` run per VOD.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-01
"""

from alembic import op
from vex_platform import migrations

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migrations.apply(op, migrations.jobs_sql(3, schema="jobs"))


def downgrade() -> None:
    op.execute("ALTER TABLE jobs.job_runs DROP COLUMN parent_id")
