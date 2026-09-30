"""Start ``jobs.job_runs`` ids above the legacy ``jobs`` ids (additive).

New jobs are runs of vex-platform's runtime from now on; the legacy table only drains. The admin API
lists both as one (``archive_worker.job_rows``) and addresses a job by its id alone, so the two must
not share ids. The gap of 1000 leaves room for a job queued by a worker still on the old code while
this deploys.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-30
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "SELECT setval('jobs.job_runs_id_seq', GREATEST("
        " (SELECT COALESCE(max(id), 0) FROM public.jobs) + 1000,"
        " (SELECT COALESCE(max(id), 0) FROM jobs.job_runs) + 1))"
    )


def downgrade() -> None:
    pass  # the ids stay where they are
