"""vex-platform tables: the ``jobs`` schema and ``audit_log`` (additive).

``jobs`` (a Postgres schema, not the legacy ``public.jobs`` table) holds procrastinate 3.10.0's tables
plus ``job_runs`` and ``job_run_events``, the job runs vex-platform's ``JobRuntime`` executes.
``audit_log`` is the audit table shared with doomtp-bot (see vex-platform's docs/conventions.md).

Nothing reads or writes them yet: the legacy ``jobs``/``job_events``/``admin_audit`` tables stay in
use until the code moves over, and ``admin_audit``'s rows are copied into ``audit_log`` by the
revision that switches the writer, so none written in between are missed.

The SQL is vex-platform's, frozen per revision: ``jobs_sql(1)`` creates the same schema whichever
vex-platform version is installed.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-30
"""

from alembic import op
from vex_platform import migrations

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migrations.apply(op, migrations.jobs_sql(1, schema="jobs"))
    migrations.apply(op, migrations.audit_sql(1, table="public.audit_log"))


def downgrade() -> None:
    op.execute("DROP TABLE public.audit_log")
    op.execute("DROP SCHEMA jobs CASCADE")
