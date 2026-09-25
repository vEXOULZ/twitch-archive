"""jobs.not_before: retry backoff as a real column instead of a payload key.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("not_before", sa.DateTime(timezone=True)))
    op.execute(
        "UPDATE jobs SET not_before = (payload->>'not_before')::timestamptz, payload = payload - 'not_before' "
        "WHERE payload ? 'not_before'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE jobs SET payload = payload || jsonb_build_object('not_before', to_char("
        "not_before AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"')) "
        "WHERE not_before IS NOT NULL"
    )
    op.drop_column("jobs", "not_before")
