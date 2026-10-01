"""Validation for hand edits of a VOD (admin API): its fields (title, hidden, thumbnail,
duration, date), chapters, YouTube and Drive lists, and games rows.

Pure functions: each takes the request's list and returns what to store, or
raises ValueError with a message fit for the response.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from archive_common.serialize import box_art_image
from archive_common.timeutil import format_hhmmss, hhmmss_to_seconds

from . import planning

VIDEO_TYPES = ("vod", "live")
GAP_KIND = "gap"  # chapters[].kind: marks a merge's gap (see timeline); the only kind, else absent
OVERLAP_SLACK = 0.001  # seconds of float noise tolerated where chapters meet
DURATION_SLACK = 1.0  # stored durations are whole seconds (HH:MM:SS), so allow the lost fraction


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _list(items: Any, name: str) -> list:
    if not isinstance(items, list):
        raise ValueError(f"{name} must be a list")
    return items


def _object(item: Any, where: str, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be an object")
    missing = sorted(required - item.keys())
    if missing:
        raise ValueError(f"{where} is missing {', '.join(missing)}")
    unknown = sorted(item.keys() - required - optional)
    if unknown:
        raise ValueError(f"{where} has unknown field(s) {', '.join(unknown)}")
    return item


def _optional_str(item: dict, key: str, where: str) -> str | None:
    value = item.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{where}.{key} must be a string or null")
    return value


def chapters(items: Any, duration: float) -> list[dict[str, Any]]:
    """Admin chapters -> the stored shape (see planning): sorted by start, no overlaps,
    every length > 0, and inside ``duration`` seconds (unchecked when that is 0/unknown).
    ``kind`` is kept when given (a gap chapter stays one)."""
    out: list[dict[str, Any]] = []
    prev_start = prev_end = None
    for i, item in enumerate(_list(items, "chapters")):
        where = f"chapters[{i}]"
        ch = _object(item, where, {"name", "gameId", "start", "length", "restricted"}, {"imageTemplate", "kind"})
        name = _optional_str(ch, "name", where)
        game_id = _optional_str(ch, "gameId", where)  # Helix ids are strings
        template = _optional_str(ch, "imageTemplate", where)
        start, length = ch["start"], ch["length"]
        if not is_number(start) or start < 0:
            raise ValueError(f"{where}.start must be a number of seconds >= 0")
        if not is_number(length) or length <= 0:
            raise ValueError(f"{where}.length must be a number of seconds > 0")
        if not isinstance(ch["restricted"], bool):
            raise ValueError(f"{where}.restricted must be true or false")
        kind = ch.get("kind")
        if kind not in (None, GAP_KIND):
            raise ValueError(f"{where}.kind must be {GAP_KIND!r} or absent")
        if prev_start is not None and start < prev_start:
            raise ValueError(f"{where} starts before chapters[{i - 1}]; sort chapters by start")
        if prev_end is not None and start < prev_end - OVERLAP_SLACK:
            raise ValueError(f"{where} starts at {start}s, inside chapters[{i - 1}] (which ends at {prev_end}s)")
        if duration > 0 and start + length > duration + DURATION_SLACK:
            raise ValueError(f"{where} ends at {start + length}s, after the end of the VOD ({duration:g}s)")
        prev_start, prev_end = start, start + length
        out.append({
            **planning.chapter(game_id, name, box_art_image(template), start, length, ch["restricted"]),
            "imageTemplate": template,
            **({"kind": kind} if kind is not None else {}),
        })
    return out


def _video_type(item: dict, where: str) -> str:
    if item["type"] not in VIDEO_TYPES:
        raise ValueError(f"{where}.type must be 'vod' or 'live'")
    return item["type"]


def _video_id(item: dict, where: str) -> str:
    if not isinstance(item["id"], str) or not item["id"].strip():
        raise ValueError(f"{where}.id must be a non-empty string")
    return item["id"].strip()


def youtube(items: Any, existing: list[dict] | None) -> list[dict[str, Any]]:
    """Admin YouTube list -> vods.youtube. A video already listed keeps its thumbnail
    (and its duration, unless a new one is given)."""
    before = {e.get("id"): e for e in existing or [] if isinstance(e, dict)}
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_parts: set[tuple[str, int]] = set()
    for i, item in enumerate(_list(items, "youtube")):
        where = f"youtube[{i}]"
        _object(item, where, {"id", "type", "part"}, {"duration"})
        video_id, typ = _video_id(item, where), _video_type(item, where)
        part = item["part"]
        if not isinstance(part, int) or isinstance(part, bool) or part < 1:
            raise ValueError(f"{where}.part must be a whole number >= 1")
        if video_id in seen_ids:
            raise ValueError(f"{where}: video {video_id} is listed twice")
        if (typ, part) in seen_parts:
            raise ValueError(f"{where}: there is already a {typ} part {part}")
        seen_ids.add(video_id)
        seen_parts.add((typ, part))
        old = before.get(video_id, {})
        entry: dict[str, Any] = {"id": video_id, "type": typ}
        duration = item.get("duration", old.get("duration"))
        if duration is not None:
            if not is_number(duration) or duration < 0:
                raise ValueError(f"{where}.duration must be a number of seconds >= 0")
            entry["duration"] = planning.num_seconds(duration)
        entry["part"] = part
        entry["thumbnail_url"] = old.get("thumbnail_url") or planning.youtube_thumbnail(video_id)
        out.append(entry)
    return out


def drive(items: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, item in enumerate(_list(items, "drive")):
        where = f"drive[{i}]"
        _object(item, where, {"id", "type"})
        file_id, typ = _video_id(item, where), _video_type(item, where)
        if file_id in seen:
            raise ValueError(f"{where}: file {file_id} is listed twice")
        seen.add(file_id)
        out.append({"id": file_id, "type": typ})
    return out


# ── VOD fields (PATCH /admin/vods/{id}) ──────────────────────────────────

# Request key -> vods column. A merged VOD takes only MERGED_EDITABLE (its content is the other VOD's now).
FIELDS = {"title": "title", "hidden": "hidden", "thumbnailUrl": "thumbnail_url", "duration": "duration",
          "createdAt": "created_at"}
MERGED_EDITABLE = {"hidden"}
_HHMMSS = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")


def http_url(value: Any, where: str) -> str | None:
    """An http(s) URL, or None for null / empty."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a URL or null")
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{where} must be an http(s) URL or null")
    return value.strip()


def duration(value: Any) -> str:
    """``HH:MM:SS`` (hours may run past 99), stored zero-padded."""
    match = _HHMMSS.match(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("duration must be HH:MM:SS")
    return format_hhmmss(hhmmss_to_seconds(value))


def created_at(value: Any) -> dt.datetime:
    """An ISO 8601 date and time with its offset (``Z`` or ``+hh:mm``)."""
    try:
        when = dt.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        when = None
    if when is None or when.tzinfo is None:
        raise ValueError("createdAt must be an ISO date and time with an offset, e.g. 2026-09-30T18:00:00Z")
    return when.astimezone(dt.timezone.utc)


def vod_fields(body: dict) -> dict[str, Any]:
    """PATCH body -> the vods columns to set. Unknown keys are refused."""
    unknown = sorted(set(body) - FIELDS.keys())
    if unknown:
        raise ValueError(f"unknown field(s) {', '.join(unknown)}; these can be changed: {', '.join(FIELDS)}")
    out: dict[str, Any] = {}
    if "title" in body:
        if not isinstance(body["title"], str) or not body["title"].strip():
            raise ValueError("title must be a non-empty string")
        out["title"] = body["title"].strip()
    if "hidden" in body:
        if not isinstance(body["hidden"], bool):
            raise ValueError("hidden must be true or false")
        out["hidden"] = body["hidden"]
    if "thumbnailUrl" in body:
        out["thumbnail_url"] = http_url(body["thumbnailUrl"], "thumbnailUrl")
    if "duration" in body:
        out["duration"] = duration(body["duration"])
    if "createdAt" in body:
        out["created_at"] = created_at(body["createdAt"])
    return out


def check_fits(chapters: Any, games_end: float, seconds: float) -> None:
    """A new duration must still hold the VOD's chapters and games rows (ValueError otherwise)."""
    for what, end in (("chapters", content_end(chapters)), ("games rows", games_end)):
        if end > seconds + DURATION_SLACK:
            raise ValueError(f"The {what} run to {end:g}s, past the new duration ({seconds:g}s); shorten them first")


def content_end(chapters: Any) -> float:
    """Where the last chapter ends, in seconds (``end`` holds each chapter's length)."""
    ends = [c["start"] + c["end"] for c in chapters or []
            if isinstance(c, dict) and is_number(c.get("start")) and is_number(c.get("end"))]
    return max(ends, default=0)


# ── Games rows (PUT /admin/vods/{id}/games) ──────────────────────────────

GAME_TEXT = ("game_id", "game_name", "title", "video_provider", "video_id")
GAME_URLS = ("thumbnail_url", "chapter_image")
GAME_READ_ONLY = {"id", "vodId", "createdAt", "updatedAt"}  # as GET returns them; ignored


def _seconds(value: Any, where: str) -> float:
    """A number of seconds; the numeric strings GET returns are taken too."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            pass
    if not is_number(value) or value < 0:
        raise ValueError(f"{where} must be a number of seconds >= 0")
    return value


def games(items: Any, vod_duration: float) -> list[dict[str, Any]]:
    """Admin games rows (the shape GET /games renders) -> column values, sorted by start, no
    overlaps, each inside ``vod_duration`` seconds (unchecked when that is 0/unknown)."""
    out: list[dict[str, Any]] = []
    prev_start = prev_end = None
    for i, item in enumerate(_list(items, "games")):
        where = f"games[{i}]"
        row = _object(item, where, {"start_time", "end_time", "game_name"},
                      {*GAME_TEXT, *GAME_URLS} | GAME_READ_ONLY)
        start, end = _seconds(row["start_time"], f"{where}.start_time"), _seconds(row["end_time"], f"{where}.end_time")
        if end <= start:
            raise ValueError(f"{where} must end after it starts")
        if prev_start is not None and start < prev_start:
            raise ValueError(f"{where} starts before games[{i - 1}]; sort games by start_time")
        if prev_end is not None and start < prev_end - OVERLAP_SLACK:
            raise ValueError(f"{where} starts at {start:g}s, inside games[{i - 1}] (which ends at {prev_end:g}s)")
        if vod_duration > 0 and end > vod_duration + DURATION_SLACK:
            raise ValueError(f"{where} ends at {end:g}s, after the end of the VOD ({vod_duration:g}s)")
        if not (_optional_str(row, "game_name", where) or "").strip():
            raise ValueError(f"{where}.game_name must be a non-empty string")
        prev_start, prev_end = start, end
        out.append({
            "start_time": Decimal(str(planning.num_seconds(start))),
            "end_time": Decimal(str(planning.num_seconds(end))),
            **{k: _optional_str(row, k, where) for k in GAME_TEXT},
            **{k: http_url(row.get(k), f"{where}.{k}") for k in GAME_URLS},
        })
    return out
