"""Worker settings the admin dashboard can change while the worker runs (Alembic 0011's ``settings``).

Each entry is a ``Settings`` field. Its env value (or the default) stays the fallback; a row in
``settings`` overrides it. The worker has one ``Settings`` object that the monitor, the runner and
the admin API all read, and ``RuntimeSettings`` writes the effective values into it at start and
after every change. ``applies`` says when a change is seen:

- ``now``: read on every use (the monitor's next check, the runner's next pick, the next step gate);
- ``next job``: a job reads its settings when it starts or resumes (``JobContext.settings``).

Secrets, keys, URLs, GQL hashes, paths, the channel and the database settings are never here.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from archive_common import audit
from archive_common.config import Settings
from archive_common.db import get_sessionmaker
from archive_common.models import RuntimeSetting
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from vex_platform.audit import AuditEntry
from vex_platform.audit.sqlalchemy import record

from . import jobs
from .events import iso_utc

log = logging.getLogger(__name__)

TEXT_MAX = 500

# (before, after) -> the audit row a change writes in its own transaction (the v2 routes).
Audit = Callable[[dict[str, Any], dict[str, Any]], AuditEntry]


@dataclass(frozen=True)
class Entry:
    key: str
    type: str  # bool | int | float | text | list | steps (per job kind)
    group: str  # Capture | YouTube | Pipeline | Runner
    applies: str  # now | next job
    help: str
    min: float | None = None
    max: float | None = None
    requires: str | None = None  # a bool setting this one does nothing without


ENTRIES = (
    Entry("vod_download", "bool", "Capture", "now", "Archive every stream's Twitch VOD"),
    Entry("chat_download", "bool", "Capture", "next job", "Save the chat replay"),
    Entry("live_record", "bool", "Capture", "now", "Record the live stream itself"),
    Entry(
        "multi_track",
        "bool",
        "Capture",
        "now",
        "Also upload the VOD copy, unlisted, next to the live copy",
        requires="live_record",
    ),
    Entry(
        "monitor_interval_seconds", "int", "Capture", "now", "How often Twitch is checked for a live stream", 5, 3600
    ),
    Entry("youtube_upload", "bool", "YouTube", "next job", "Upload to YouTube"),
    Entry(
        "youtube_public",
        "bool",
        "YouTube",
        "next job",
        "Public instead of unlisted (the main copy: the live one when live_record is on)",
    ),
    Entry("youtube_description", "text", "YouTube", "next job", "Last line of every description"),
    Entry(
        "youtube_keepalive_hours",
        "float",
        "YouTube",
        "now",
        "How often the YouTube token is refreshed (from the next refresh)",
        1,
        720,
    ),
    Entry("restricted_games", "list", "Pipeline", "next job", "Chapters of these games are left out of uploads"),
    Entry("split_duration", "int", "Pipeline", "next job", "Maximum YouTube part length in seconds", 600, 43200),
    Entry("keep_hls", "bool", "Pipeline", "next job", "Keep the HLS segments after upload"),
    Entry("keep_mp4", "bool", "Pipeline", "next job", "Keep the MP4 after upload"),
    Entry("previews", "bool", "Pipeline", "next job", "Make seek-bar previews of each uploaded part"),
    Entry(
        "previews_fetch_pause_seconds",
        "int",
        "Pipeline",
        "next job",
        "Pause between two YouTube downloads of the previews backfill",
        0,
        3600,
    ),
    Entry(
        "manual_steps",
        "steps",
        "Pipeline",
        "now",
        "Steps a job pauses before until resumed, per job kind (a job's own list wins)",
    ),
    Entry("runner_concurrency", "int", "Runner", "now", "Jobs run at once", 1, 16),
    Entry("max_attempts", "int", "Runner", "now", "Tries of a failing step before its job fails", 1, 10),
)
BY_KEY = {e.key: e for e in ENTRIES}


def validate(key: str, value: Any) -> Any:
    """The value to store for ``key``, or ValueError saying what is wrong."""
    entry = BY_KEY.get(key)
    if entry is None:
        raise ValueError(f"{key} can't be changed here; settings: {', '.join(BY_KEY)}")
    t = entry.type
    if t == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if t in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be a number")
        if t == "int" and value != int(value):
            raise ValueError(f"{key} must be a whole number")
        value = int(value) if t == "int" else float(value)
        if (entry.min is not None and value < entry.min) or (entry.max is not None and value > entry.max):
            raise ValueError(f"{key} must be between {entry.min:g} and {entry.max:g}")
        return value
    if t == "text":
        if not isinstance(value, str) or len(value) > TEXT_MAX:
            raise ValueError(f"{key} must be text of at most {TEXT_MAX} characters")
        return value
    if t == "list":
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ValueError(f"{key} must be a list of non-empty names")
        return list(dict.fromkeys(v.strip() for v in value))
    # steps: {kind: [step, ...]}
    if not isinstance(value, dict) or not all(
        isinstance(v, list) and all(isinstance(s, str) for s in v) for v in value.values()
    ):
        raise ValueError(f"{key} must map job kinds to lists of steps")
    try:
        for kind, steps in value.items():
            jobs.check_steps(kind, steps)
    except jobs.InvalidJob as exc:
        raise ValueError(f"{key}: {exc}") from exc
    return {kind: list(dict.fromkeys(steps)) for kind, steps in value.items() if steps}


class RuntimeSettings:
    """The overrides, applied onto ``settings`` (the worker's one ``Settings`` object)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.env = {e.key: copy.deepcopy(getattr(settings, e.key)) for e in ENTRIES}
        self.overrides: dict[str, RuntimeSetting] = {}

    async def load(self) -> None:
        """Read the table and apply it. A row that no longer validates is logged and left out."""
        async with get_sessionmaker()() as s:
            rows = (await s.execute(select(RuntimeSetting))).scalars().all()
        overrides = {}
        for row in rows:
            try:
                row.value = validate(row.key, row.value)
            except ValueError as exc:
                log.warning("ignoring the saved setting %s: %s", row.key, exc)
                continue
            overrides[row.key] = row
        self.overrides = overrides
        for e in ENTRIES:
            value = overrides[e.key].value if e.key in overrides else self.env[e.key]
            setattr(self.settings, e.key, copy.deepcopy(value))

    def current(self, keys) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return {k: copy.deepcopy(getattr(self.settings, k)) for k in keys}

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for e in ENTRIES:
            row = self.overrides.get(e.key)
            item = {
                "key": e.key,
                "value": getattr(self.settings, e.key),
                "default": self.env[e.key],
                "overridden": row is not None,
                "type": e.type,
                "group": e.group,
                "applies": e.applies,
                "help": e.help,
                "min": e.min,
                "max": e.max,
                "updatedAt": iso_utc(row.updated_at) if row else None,
                "updatedBy": row.updated_by if row else None,
            }
            if e.type == "steps":
                item["choices"] = jobs.KINDS
            if e.requires:
                item["requires"] = e.requires
            out.append(item)
        return out

    async def update(
        self, changes: dict[str, Any], by: str, audit_as: Audit | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Override every key in ``changes``, or none (ValueError). Returns the values before and after."""
        if not changes:
            raise ValueError("Send at least one setting")
        values = {k: validate(k, v) for k, v in changes.items()}
        before = self.current(values)
        now = dt.datetime.now(dt.UTC)
        async with get_sessionmaker()() as s:
            for key, value in values.items():
                stmt = insert(RuntimeSetting).values(key=key, value=value, updated_at=now, updated_by=by)
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[RuntimeSetting.key],
                        set_={"value": stmt.excluded.value, "updated_at": now, "updated_by": by},
                    )
                )
            if audit_as:
                await record(s, audit_as(before, copy.deepcopy(values)), table=audit.TABLE)
            await s.commit()
        await self.load()
        return before, self.current(values)

    async def reset(self, key: str, audit_as: Audit | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """Back to the env value. Returns the value before and after."""
        if key not in BY_KEY:
            raise KeyError(key)
        before = self.current([key])
        async with get_sessionmaker()() as s:
            await s.execute(delete(RuntimeSetting).where(RuntimeSetting.key == key))
            if audit_as:
                await record(s, audit_as(before, {key: copy.deepcopy(self.env[key])}), table=audit.TABLE)
            await s.commit()
        await self.load()
        return before, self.current([key])
