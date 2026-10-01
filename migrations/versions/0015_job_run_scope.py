"""vex-platform 0.3.0's ``job_runs.scope`` column (additive).

vex-platform 0.3.0 keeps the scope a run was queued with and gives it to every audit row about the run,
not only ``job.enqueue``'s. It reads the column, so this runs before that version starts; an image still
on 0.2.0 ignores it. The archive queues runs without a scope (it has one channel), so it stays NULL here.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-01
"""

from alembic import op
from vex_platform import migrations

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migrations.apply(op, migrations.jobs_sql(2, schema="jobs"))


def downgrade() -> None:
    op.execute("ALTER TABLE jobs.job_runs DROP COLUMN scope")
