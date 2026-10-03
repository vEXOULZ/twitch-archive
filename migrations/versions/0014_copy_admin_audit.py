"""Copy ``admin_audit`` into ``audit_log`` (additive: ``admin_audit`` stays, no longer written).

From this release the admin API audits into ``audit_log`` (archive_common/audit.py), with dotted
actions (``"PATCH /admin/vods/{vod_id}"`` becomes ``vod.update``) and before/after in their own
columns. Each copy keeps the old id as ``request_id = "admin_audit:<id>"``. The previous release keeps
writing ``admin_audit`` until its containers stop, after this runs; the worker copies those rows when
it starts (``copy_admin_audit``), and skips the ones already copied.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-30
"""

from alembic import op
from archive_common.audit import copy_admin_audit_sync

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    copy_admin_audit_sync(op.get_bind())


def downgrade() -> None:
    op.execute("DELETE FROM public.audit_log WHERE request_id LIKE 'admin_audit:%'")
