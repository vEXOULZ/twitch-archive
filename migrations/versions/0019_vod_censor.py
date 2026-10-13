"""Edits applied to a VOD's uploads (additive): ``vods.censor``.

A list of ``{"start", "end", "video", "mute", "note"}`` ranges in VOD seconds, set from the admin
dashboard: the ``censor`` step blurs or blacks out the video (``video``: "blur" or "black") and
silences the audio (``mute``) there before the parts are cut and uploaded. Only uploads made after
the ranges are set carry them; the dashboard re-archives a VOD to replace the older ones.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vods", sa.Column("censor", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    op.drop_column("vods", "censor")
