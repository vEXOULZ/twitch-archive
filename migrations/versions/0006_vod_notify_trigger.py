"""NOTIFY vods_changed from the database on every vods/games write; index games.vod_id (additive).

Until now the worker's admin routes sent the NOTIFY themselves, so every other
writer (job steps, the monitor) left archive-api serving stale VODs until the
cache TTL ran out. A trigger covers every writer. Updates that change nothing
but ``updatedAt`` send nothing.

``attach_games`` filters ``games.vod_id IN (...)`` on every uncached VOD
response; the legacy schema has no index for it.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# Payload: the id of the VOD whose cached API responses are stale (archive_common.db.VOD_CHANGED).
FUNCTION = """
CREATE OR REPLACE FUNCTION archive_notify_vod_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_id text;
    new_id text;
BEGIN
    IF TG_TABLE_NAME = 'vods' THEN
        IF TG_OP <> 'INSERT' THEN old_id := OLD.id; END IF;
        IF TG_OP <> 'DELETE' THEN new_id := NEW.id; END IF;
    ELSE
        IF TG_OP <> 'INSERT' THEN old_id := OLD.vod_id; END IF;
        IF TG_OP <> 'DELETE' THEN new_id := NEW.vod_id; END IF;
    END IF;
    IF new_id IS NOT NULL THEN
        PERFORM pg_notify('vods_changed', new_id);
    END IF;
    IF old_id IS NOT NULL AND old_id IS DISTINCT FROM new_id THEN
        PERFORM pg_notify('vods_changed', old_id);
    END IF;
    RETURN NULL;
END
$$
"""

TABLES = ("vods", "games")
# An update notifies only when something besides "updatedAt" (set on every ORM update) changed.
UNCHANGED_IGNORED = "(to_jsonb(OLD) - 'updatedAt') IS DISTINCT FROM (to_jsonb(NEW) - 'updatedAt')"


def upgrade() -> None:
    op.execute(FUNCTION)
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_notify_changed AFTER INSERT OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION archive_notify_vod_changed()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_notify_updated AFTER UPDATE ON {table} "
            f"FOR EACH ROW WHEN ({UNCHANGED_IGNORED}) EXECUTE FUNCTION archive_notify_vod_changed()"
        )

    # Skip the index if the database already has one led by games.vod_id (under any name).
    indexed = op.get_bind().execute(sa.text(
        "SELECT 1 FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
        "WHERE i.indrelid = 'games'::regclass AND a.attname = 'vod_id'"
    )).first()
    if indexed is None:
        with op.get_context().autocommit_block():
            op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS games_vod_id_idx ON games (vod_id)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS games_vod_id_idx")
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_notify_updated ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_notify_changed ON {table}")
    op.execute("DROP FUNCTION IF EXISTS archive_notify_vod_changed()")
