"""Synthetic VODs composed from windows of real ones (see ``archive_common.segments``).

Pure functions, no I/O, like ``timeline``. A merge, a split or a playthrough is a list of segments;
the real VODs behind it are never changed, so their jobs keep running and the synthetic VOD follows
them (``synthetic.recompose``). ``derive`` gives the synthetic VOD's own ``vods`` columns, a cache
of its sources' that lists, search and status read like any VOD's:

* ``chapters``: each segment's window of its source's chapters, moved to where the segment is; a
  space between two segments is a gap chapter (restricted, so the player skips it as it skips a
  merge's gap);
* ``duration``: where the last segment ends, in whole seconds (rounded up);
* ``created_at``: when the first segment's footage was live; ``thumbnail_url``: its source's.

The site plays each segment with its source's own timeline (uploads, delay, cuts), so nothing
here touches uploads: a segment may start or end anywhere, mid-upload included.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from archive_common.segments import EPS, Segment, resolve, total
from archive_common.timeutil import format_hhmmss

from . import timeline
from .vod_edits import is_number

MAX_SEGMENTS = 200
LABEL_MAX = 200
# A synthetic VOD's id: never all digits, so it never takes a Twitch VOD's.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9+_-]{0,99}$")


class ComposeError(ValueError):
    """The segments would not make a VOD; ``extra`` goes into the response."""

    def __init__(self, msg: str, **extra: Any) -> None:
        super().__init__(msg)
        self.extra = extra


@dataclass(frozen=True)
class Source:
    """What composing needs of a VOD (``synthetic.source_of`` reads it from a row)."""

    id: str
    duration: float
    chapters: list
    created_at: dt.datetime
    thumbnail_url: str | None = None
    title: str | None = None
    synthetic: bool = False


def check_id(vod_id: Any) -> str:
    if not isinstance(vod_id, str) or not ID_RE.match(vod_id) or vod_id.isdigit():
        raise ComposeError("id must be 1-100 of a-z, 0-9, '+', '_' and '-', starting with a letter or digit, "
                           "and not only digits (those are Twitch's)")
    return vod_id


def _number(item: dict, key: str, where: str, *, optional: bool = False) -> float | None:
    value = item.get(key)
    if value is None and optional:
        return None
    if not is_number(value) or value < 0:
        raise ComposeError(f"{where}.{key} must be a number of seconds >= 0" + (" or null" if optional else ""))
    return float(value)


def parse(items: Any) -> list[Segment]:
    """Request segments ``[{vodId, start?, end?, at?, label?}]`` -> segments. ``start`` defaults to 0,
    ``end`` to the source's end; a missing ``at`` puts the segment right after the one before it
    (which then needs an ``end``), so a playthrough can list windows without placing them."""
    if not isinstance(items, list) or not items:
        raise ComposeError("segments must be a non-empty list")
    if len(items) > MAX_SEGMENTS:
        raise ComposeError(f"at most {MAX_SEGMENTS} segments")
    out: list[Segment] = []
    for i, item in enumerate(items):
        where = f"segments[{i}]"
        if not isinstance(item, dict):
            raise ComposeError(f"{where} must be an object")
        unknown = sorted(item.keys() - {"vodId", "start", "end", "at", "label"})
        if unknown:
            raise ComposeError(f"{where} has unknown field(s) {', '.join(unknown)}")
        if not isinstance(item.get("vodId"), str) or not item["vodId"]:
            raise ComposeError(f"{where}.vodId must be a VOD id")
        start = _number(item, "start", where, optional=True) or 0.0
        end = _number(item, "end", where, optional=True)
        if end is not None and end <= start + EPS:
            raise ComposeError(f"{where} must end after it starts")
        at = _number(item, "at", where, optional=True)
        if at is None:
            if not out:
                at = 0.0
            elif out[-1].end is None:
                raise ComposeError(f"{where} has no 'at', and segments[{i - 1}] has no 'end' to put it after")
            else:
                at = out[-1].at + out[-1].length
        label = item.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > LABEL_MAX):
            raise ComposeError(f"{where}.label must be a string of at most {LABEL_MAX} characters, or null")
        out.append(Segment(item["vodId"], start, end, at, (label or "").strip() or None))
    return out


def validate(segments: list[Segment], sources: Mapping[str, Source]) -> None:
    """The segments make a VOD: real sources (no synthetic of synthetics: list its segments instead),
    each window inside its source, the first one at 0, in ``at`` order without overlapping."""
    if not segments:
        raise ComposeError("A synthetic VOD needs at least one segment")
    if segments[0].at > EPS:
        raise ComposeError("segments[0] must be at 0")
    for i, seg in enumerate(segments):
        where = f"segments[{i}]"
        src = sources.get(seg.source_id)
        if src is None:
            raise ComposeError(f"{where}: no VOD {seg.source_id}", vodId=seg.source_id)
        if src.synthetic:
            raise ComposeError(f"{where}: {src.id} is a synthetic VOD; use its segments instead", vodId=src.id)
        if src.duration > 0 and seg.start >= src.duration - EPS:
            raise ComposeError(f"{where} starts at {seg.start:g}s, at or after the end of {src.id} "
                               f"({src.duration:g}s)", vodId=src.id)
        if src.duration > 0 and seg.end is not None and seg.end > src.duration + 1:  # durations are whole seconds
            raise ComposeError(f"{where} ends at {seg.end:g}s, after the end of {src.id} ({src.duration:g}s)",
                               vodId=src.id)
        if i:
            prev = segments[i - 1]
            if seg.at < prev.at:
                raise ComposeError(f"{where} is at {seg.at:g}s, before segments[{i - 1}]; sort segments by at")
            prev_end = prev.end if prev.end is not None else sources[prev.source_id].duration
            if prev.at + prev_end - prev.start > seg.at + EPS:
                raise ComposeError(f"{where} is at {seg.at:g}s, inside segments[{i - 1}] (which runs to "
                                   f"{prev.at + prev_end - prev.start:g}s)")


def derive(segments: list[Segment], sources: Mapping[str, Source]) -> dict[str, Any]:
    """The synthetic VOD's cached ``vods`` columns: duration, chapters, created_at, thumbnail_url."""
    resolved = resolve(segments, {k: v.duration for k, v in sources.items()})
    chapters: list[dict] = []
    end = 0.0
    for seg in resolved:
        if seg.at - end > EPS:
            chapters.append(timeline.gap_chapter(end, seg.at - end))
        if seg.length > EPS:
            chapters += timeline.shift(timeline.clip(sources[seg.source_id].chapters, seg.start, seg.end),
                                       seg.at - seg.start)
        end = max(end, seg.at + seg.length)
    first = sources[segments[0].source_id] if segments else None
    return {
        "duration": format_hhmmss(math.ceil(total(resolved) - EPS)),
        "chapters": chapters,
        "created_at": first.created_at + dt.timedelta(seconds=segments[0].start) if first else None,
        "thumbnail_url": first.thumbnail_url if first else None,
    }


# ── Builders ──────────────────────────────────────────────────────────────


def merge_segments(a: Source, b: Source, gap: Any = None) -> list[Segment]:
    """``a`` then ``b`` (a later VOD of the same broadcast), ``b`` placed where it started against ``a``
    (or ``gap`` seconds after ``a`` ends). Where they overlap, ``a`` stops where ``b`` starts."""
    if a.id == b.id:
        raise ComposeError("A VOD cannot be merged with itself")
    if b.created_at < a.created_at:
        raise ComposeError(f"{b.id} started before {a.id}; merge them the other way round")
    if gap is None:
        offset = float(round((b.created_at - a.created_at).total_seconds()))
    elif not is_number(gap) or gap < 0:
        raise ComposeError("gap must be a number of seconds >= 0")
    else:
        offset = a.duration + float(gap)
    if offset <= EPS:
        raise ComposeError(f"{b.id} starts with {a.id}; there is nothing of {a.id} to keep")
    return [Segment(a.id, 0.0, None if offset >= a.duration else offset, 0.0), Segment(b.id, 0.0, None, offset)]


def split_segments(vod: Source, at: Any) -> tuple[list[Segment], list[Segment]]:
    """The two halves of ``vod`` around ``at`` seconds (anywhere: uploads are not cut, only played apart)."""
    if not is_number(at) or not EPS < at < vod.duration - EPS:
        raise ComposeError(f"at must be a number of seconds inside {vod.id} (0-{vod.duration:g})")
    return [Segment(vod.id, 0.0, float(at), 0.0)], [Segment(vod.id, float(at), None, 0.0)]


def split_ids(vod_id: str) -> tuple[str, str]:
    return f"{vod_id}-1", f"{vod_id}-2"


def merge_id(a_id: str, b_id: str) -> str:
    return f"{a_id}+{b_id}"


def game_windows(chapters: Any, game_id: str, join_within: float = 1.0) -> list[tuple[float, float]]:
    """Where a VOD plays ``game_id``: its chapters of that game, those less than ``join_within``
    seconds apart joined (a playthrough's candidate windows; cut chapters are left out)."""
    out: list[tuple[float, float]] = []
    for ch in sorted(timeline._chapters(chapters), key=timeline.start_of):
        if str(ch.get("gameId")) != game_id or ch.get("restricted") is True or timeline.length_of(ch) <= EPS:
            continue
        s, e = timeline.start_of(ch), timeline.start_of(ch) + timeline.length_of(ch)
        if out and s - out[-1][1] < join_within:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out
