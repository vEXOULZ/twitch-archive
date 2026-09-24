"""Legacy Feathers/Sequelize schema (baseline).

Creates the legacy tables only when they do not exist, so this is a no-op on
the production database (which Sequelize created) and gives fresh dev/test
databases the identical schema.

Revision ID: 0000
Revises:
Create Date: 2026-09-23
"""

from alembic import op

revision = "0000"
down_revision = None
branch_labels = None
depends_on = None


LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS vods (
    id text PRIMARY KEY,
    chapters jsonb DEFAULT '[]'::jsonb,
    title text,
    duration text DEFAULT '00:00:00'::text,
    thumbnail_url text,
    youtube jsonb DEFAULT '[]'::jsonb,
    stream_id text,
    drive jsonb DEFAULT '[]'::jsonb,
    platform text NOT NULL,
    "createdAt" timestamptz NOT NULL,
    "updatedAt" timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    id bigserial PRIMARY KEY,
    vod_id text NOT NULL REFERENCES vods(id) ON UPDATE CASCADE ON DELETE CASCADE,
    start_time numeric,
    end_time numeric,
    video_provider text,
    video_id text,
    thumbnail_url text,
    game_id text,
    game_name text,
    title text,
    chapter_image text,
    "createdAt" timestamptz NOT NULL,
    "updatedAt" timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS emotes (
    vod_id text PRIMARY KEY REFERENCES vods(id) ON UPDATE CASCADE ON DELETE CASCADE,
    ffz_emotes jsonb DEFAULT '[]'::jsonb,
    bttv_emotes jsonb DEFAULT '[]'::jsonb,
    "7tv_emotes" jsonb DEFAULT '[]'::jsonb,
    "createdAt" timestamptz NOT NULL,
    "updatedAt" timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id uuid PRIMARY KEY,
    _id serial NOT NULL,
    vod_id text NOT NULL,
    display_name text,
    content_offset_seconds integer NOT NULL,
    message jsonb NOT NULL,
    user_badges jsonb NOT NULL,
    user_color text NOT NULL,
    "createdAt" timestamptz NOT NULL,
    "updatedAt" timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS streams (
    id bigint PRIMARY KEY,
    started_at timestamptz,
    platform text NOT NULL,
    is_live boolean
);
"""


def upgrade() -> None:
    # asyncpg runs one statement per call
    for statement in LEGACY_SCHEMA.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    # Never drop the legacy data.
    pass
