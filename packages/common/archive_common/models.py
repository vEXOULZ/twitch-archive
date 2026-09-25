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
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()


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


class AdminAudit(Base):
    """One row per state-changing admin request (Alembic 0005)."""

    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now())
    actor: Mapped[str] = mapped_column(Text, nullable=False)  # password | api-key
    action: Mapped[str] = mapped_column(Text, nullable=False)  # "<METHOD> <route>"
    target: Mapped[str | None] = mapped_column(Text)  # "vod:<id>" | "job:<id>"
    detail: Mapped[Any] = mapped_column(JSONB(none_as_null=True))
