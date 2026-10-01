"""ORM mapping of the existing Feathers/Sequelize schema plus the new jobs tables.

The legacy tables (vods, games, emotes, logs, streams) are mapped exactly as
Sequelize created them: camelCase ``createdAt``/``updatedAt`` columns with no
database default, the ``"7tv_emotes"`` column, and ``logs._id`` as a serial
that is *not* the primary key. Never ``create_all`` against production; use
Alembic (see migrations/).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    FetchedValue,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _created() -> Any:
    return mapped_column("createdAt", DateTime(timezone=True), nullable=False, default=func.now())


def _updated() -> Any:
    return mapped_column(
        "updatedAt", DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now()
    )


class Vod(Base):
    __tablename__ = "vods"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    chapters: Mapped[list | None] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    title: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[str | None] = mapped_column(Text, server_default=text("'00:00:00'::text"), default="00:00:00")
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    youtube: Mapped[list | None] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    stream_id: Mapped[str | None] = mapped_column(Text)
    drive: Mapped[list | None] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    platform: Mapped[str] = mapped_column(Text, nullable=False, default="twitch")
    # Alembic 0005: chapters edited by hand; the automatic chapters step leaves them alone.
    chapters_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"),
                                                  default=False)
    # Alembic 0007: {"id": <vod id>, "offset": <seconds>} once merged into that VOD; NULL otherwise.
    merged_into: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    # Alembic 0008: the last bot_chat read, {"fetched_at", "since", "until", "keyed", "rows", "coverage"}; NULL = never run.
    bot_chat: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    # Alembic 0010: off the public API (lists, GET, games rows, chat, games-played, status); admin still sees it.
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), default=False)
    # Alembic 0016: NULL on a real VOD; {"supersedes": bool} on a synthetic one (its content is vod_segments,
    # and its title, duration, chapters, thumbnail and date are composed from them).
    synthetic: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    # Alembic 0016: [] = a regular VOD; a tagged one (e.g. "compilation") is listed apart.
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"),
                                            default=list)
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()


class VodSegment(Base):
    """One window of a real VOD (``source_id``, ``[start_s, end_s)``) placed at ``at_s`` on a synthetic
    VOD's timeline (Alembic 0016). ``end_s`` NULL runs to the source's end."""

    __tablename__ = "vod_segments"

    vod_id: Mapped[str] = mapped_column(
        Text, ForeignKey("vods.id", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True
    )
    pos: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("vods.id", onupdate="CASCADE", ondelete="RESTRICT"), nullable=False, index=True
    )
    start_s: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    end_s: Mapped[Decimal | None] = mapped_column(Numeric)
    at_s: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    vod_id: Mapped[str] = mapped_column(
        Text, ForeignKey("vods.id", onupdate="CASCADE", ondelete="CASCADE"), nullable=False
    )
    start_time: Mapped[Decimal | None] = mapped_column(Numeric)
    end_time: Mapped[Decimal | None] = mapped_column(Numeric)
    video_provider: Mapped[str | None] = mapped_column(Text)
    video_id: Mapped[str | None] = mapped_column(Text)
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    game_id: Mapped[str | None] = mapped_column(Text)
    game_name: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    chapter_image: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()


class Emote(Base):
    __tablename__ = "emotes"

    vod_id: Mapped[str] = mapped_column(
        Text, ForeignKey("vods.id", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True
    )
    ffz_emotes: Mapped[list | None] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    bttv_emotes: Mapped[list | None] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    seventv_emotes: Mapped[list | None] = mapped_column(
        "7tv_emotes", JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    # Alembic 0004: {"7tv": [...], "bttv": [...], "ffz": [...]}; NULL = never saved.
    global_emotes: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    global_emotes_source: Mapped[str | None] = mapped_column(Text)  # 'captured' | 'backfilled'
    global_emotes_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()


class Log(Base):
    __tablename__ = "logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # Serial used for chat pagination; filled by the database sequence.
    seq: Mapped[int] = mapped_column("_id", Integer, server_default=FetchedValue(), nullable=False)
    vod_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    content_offset_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[Any] = mapped_column(JSONB, nullable=False)
    user_badges: Mapped[Any] = mapped_column(JSONB, nullable=False)
    user_color: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()


class BotLog(Base):
    """One entry of doomtp-bot's chat log (Alembic 0008), next to the replay's ``logs``.

    ``message``, ``user_badges`` and ``user_color`` are in the replay's shape so the
    comments API serves either table the same way; ``data`` is the bot's entry as fetched.
    """

    __tablename__ = "bot_logs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # the bot's id; moderation: "mod:<n>"
    # Paging order within a VOD, like logs._id; the bot_chat step keeps it rising with the offset.
    seq: Mapped[int] = mapped_column(BigInteger, server_default=FetchedValue(), nullable=False)
    vod_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # message | notice | moderation
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_offset_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[str | None] = mapped_column(Text)
    user_login: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    message: Mapped[Any] = mapped_column(JSONB, nullable=False)
    user_badges: Mapped[Any] = mapped_column(JSONB, nullable=False)
    user_color: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cleared_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    data: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()


class Stream(Base):
    __tablename__ = "streams"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    platform: Mapped[str] = mapped_column(Text, nullable=False, default="twitch")
    is_live: Mapped[bool | None] = mapped_column(Boolean)


# ── New tables (Alembic revision 0001) ─────────────────────────────────────


class Job(Base):
    """Durable worker job. ``step`` is the next step to run; a restart resumes there."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    vod_id: Mapped[str | None] = mapped_column(Text, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # queued | running | paused | done | failed | cancelled
    state: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    step: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    not_before: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))  # retry backoff
    # Steps to pause before; NULL = Settings.manual_steps for the kind, [] = none.
    pause_before: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    pause_next: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # pause at next step
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now()
    )


class AppState(Base):
    """Small key/value store (YouTube OAuth tokens, etc.). Replaces rewriting config.json."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now()
    )


class JobEvent(Base):
    """A job's log lines and step changes (Alembic 0005), capped per job by the worker."""

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)  # "seq" in the API
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now())
    level: Mapped[str] = mapped_column(Text, nullable=False)  # info | warning | error
    step: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    progress: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))  # {done, total, unit}


class VodSplice(Base):
    """A merge of two VODs or a split of one (Alembic 0007), kept so it can be undone.

    merge: ``other_id`` was merged into ``vod_id`` at ``offset_s``; split: the part of
    ``vod_id`` from ``offset_s`` on became ``other_id``. ``snapshot`` holds the rows as
    they were before (and as the operation left them), ``detail`` the numbers it used.
    """

    __tablename__ = "vod_splices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # merge | split
    vod_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    other_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    offset_s: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now())
    undone_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class VodSpliceLog(Base):
    """The chat rows a merge moved, so the undo moves exactly those back (Alembic 0007)."""

    __tablename__ = "vod_splice_logs"

    splice_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("vod_splices.id", ondelete="CASCADE"), primary_key=True
    )
    log_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)


class VodSpliceBotLog(Base):
    """The bot chat rows a merge moved (Alembic 0008), like ``VodSpliceLog``."""

    __tablename__ = "vod_splice_bot_logs"

    splice_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("vod_splices.id", ondelete="CASCADE"), primary_key=True
    )
    bot_log_id: Mapped[str] = mapped_column(Text, primary_key=True)


class AdminAudit(Base):
    """One row per state-changing admin request (Alembic 0005)."""

    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now())
    actor: Mapped[str] = mapped_column(Text, nullable=False)  # password | api-key | twitch:<id>
    actor_login: Mapped[str | None] = mapped_column(Text)  # the Twitch login behind twitch:<id> (Alembic 0009)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # "<METHOD> <route>"
    target: Mapped[str | None] = mapped_column(Text)  # "vod:<id>" | "job:<id>"
    detail: Mapped[Any] = mapped_column(JSONB(none_as_null=True))


class AdminSession(Base):
    """A signed-in dashboard browser (Alembic 0009). Keyed by the sha256 of the cookie's token."""

    __tablename__ = "admin_sessions"

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    csrf: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)  # password | twitch:<id>
    twitch_user: Mapped[Any] = mapped_column(JSONB(none_as_null=True))
    sid: Mapped[str | None] = mapped_column(Text)  # the vexoulz-auth session behind a Twitch sign-in
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checked_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeSetting(Base):
    """A worker setting overridden from the admin dashboard (Alembic 0011); wins over the env value."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)  # a Settings field, e.g. "keep_hls"
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_by: Mapped[str | None] = mapped_column(Text)  # the Twitch login, or password / api-key
