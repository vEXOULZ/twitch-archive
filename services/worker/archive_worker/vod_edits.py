"""Validation for hand edits of a VOD's chapters, YouTube and Drive lists (admin API).

Pure functions: each takes the request's list and returns what to store, or
raises ValueError with a message fit for the response.
"""

from __future__ import annotations

import math
from typing import Any

from archive_common.serialize import box_art_image

from . import planning

VIDEO_TYPES = ("vod", "live")
CHAPTER_KINDS = ("gap",)  # chapters[].kind: "gap" marks a merge's gap (see timeline); absent otherwise
OVERLAP_SLACK = 0.001  # seconds of float noise tolerated where chapters meet
DURATION_SLACK = 1.0  # stored durations are whole seconds (HH:MM:SS), so allow the lost fraction


def _is_number(value: Any) -> bool:
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
        if not _is_number(start) or start < 0:
            raise ValueError(f"{where}.start must be a number of seconds >= 0")
        if not _is_number(length) or length <= 0:
            raise ValueError(f"{where}.length must be a number of seconds > 0")
        if not isinstance(ch["restricted"], bool):
            raise ValueError(f"{where}.restricted must be true or false")
        kind = ch.get("kind")
        if kind is not None and kind not in CHAPTER_KINDS:
            raise ValueError(f"{where}.kind must be {' or '.join(map(repr, CHAPTER_KINDS))} or absent")
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
            if not _is_number(duration) or duration < 0:
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
