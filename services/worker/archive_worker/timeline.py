"""The site's VOD timeline (vods-core ``src/timeline.ts``), and merge/split plans built on it.

Pure functions, no I/O. The site reads a VOD like this, and every plan here keeps it true:

* chapters are in VOD seconds: ``start``, and ``end``, which holds the chapter's *length*;
* a ``restricted: true`` chapter is a cut: it is in no upload, and the player skips it;
* the uploads of one type (``vod`` or ``live``) play back to back in ``part`` order, and
  none of them contains a cut;
* ``delay = duration − Σ part durations − Σ cut lengths`` is footage missing at the start
  of the VOD: upload time 0 is VOD time ``delay`` (after any cuts it has to skip).

Chat comments are placed by ``content_offset_seconds``, which is VOD time in whole seconds,
so every offset a merge or split moves rows by is a whole number of seconds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from archive_common.timeutil import format_hhmmss

from .planning import chapter, num_seconds
from .vod_edits import GAP_KIND, VIDEO_TYPES, is_number

GAP_NAME = "Technical difficulties"  # the name of every gap chapter
EPS = 0.001  # float noise tolerated where two times should meet
SPLIT_SLACK = 0.5  # a split point may be this far from a valid one: points are whole seconds


class PlanError(ValueError):
    """The operation would break the timeline; ``extra`` goes into the response."""

    def __init__(self, msg: str, **extra: Any) -> None:
        super().__init__(msg)
        self.extra = extra


# ── Chapters ──────────────────────────────────────────────────────────────


def start_of(ch: dict) -> float:
    return float(ch.get("start") or 0)


def length_of(ch: dict) -> float:
    return float(ch.get("end") or 0)  # "end" is the length, not the end time


def gap_chapter(start: float, length: float) -> dict[str, Any]:
    return {**chapter(None, GAP_NAME, None, start, length, True), "kind": GAP_KIND}


def _chapters(chapters: Any) -> list[dict]:
    return [c for c in chapters or [] if isinstance(c, dict)]


def cuts(chapters: Any) -> list[tuple[float, float]]:
    """(start, end) of every restricted chapter, sorted."""
    return sorted(
        (start_of(c), start_of(c) + length_of(c))
        for c in _chapters(chapters) if c.get("restricted") is True and length_of(c) > 0
    )


def _moved(ch: dict, start: float, length: float) -> dict:
    return {**ch, "duration": format_hhmmss(start), "start": num_seconds(start), "end": num_seconds(length)}


def clip(chapters: Any, lo: float, hi: float) -> list[dict]:
    """The part of each chapter inside [lo, hi); chapters left shorter than EPS are dropped.
    A chapter entirely inside is returned as it is."""
    out = []
    for ch in _chapters(chapters):
        s, e = start_of(ch), start_of(ch) + length_of(ch)
        if s >= lo and e <= hi:
            out.append(ch)
        elif min(e, hi) - max(s, lo) > EPS:
            out.append(_moved(ch, max(s, lo), min(e, hi) - max(s, lo)))
    return out


def shift(chapters: Any, by: float) -> list[dict]:
    return [_moved(ch, start_of(ch) + by, length_of(ch)) for ch in _chapters(chapters)]


# ── Uploads ───────────────────────────────────────────────────────────────


def upload_types(youtube: Any) -> list[str]:
    """The video types that have uploads, in VIDEO_TYPES order."""
    return [t for t in VIDEO_TYPES if any(isinstance(e, dict) and e.get("type") == t for e in youtube or [])]


def played_type(types: list[str]) -> str | None:
    """The type the site plays: ``live`` whenever a VOD has any, else ``vod``."""
    return "live" if "live" in types else (types[0] if types else None)


def parts(youtube: Any, typ: str) -> list[dict]:
    return sorted((e for e in youtube or [] if isinstance(e, dict) and e.get("type") == typ),
                  key=lambda e: e.get("part") or 0)


def renumber(*lists: Any) -> list[dict]:
    """The lists' uploads back to back, each type numbered 1..n again (vod first, like upsert)."""
    out = []
    for typ in VIDEO_TYPES:
        n = 0
        for entries in lists:
            for e in parts(entries, typ):
                n += 1
                out.append({**e, "part": n})
    return out


# ── Timeline ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Timeline:
    """One type's uploads laid over a VOD's timeline."""

    duration: float
    parts: tuple[float, ...]  # durations, in play order
    cuts: tuple[tuple[float, float], ...]

    @classmethod
    def of(cls, vod_id: str, duration: float, chapters: Any, youtube: Any, typ: str) -> Timeline:
        durations = []
        for e in parts(youtube, typ):
            if not is_number(e.get("duration")) or e["duration"] < 0:
                raise PlanError(f"{vod_id}: {typ} part {e.get('part')} ({e.get('id')}) has no duration, so its "
                                "place on the timeline is unknown; set it with PUT /admin/vods/{id}/youtube")
            durations.append(float(e["duration"]))
        return cls(float(duration), tuple(durations), tuple(cuts(chapters)))

    @property
    def delay(self) -> float:
        return self.duration - sum(self.parts) - sum(e - s for s, e in self.cuts)

    def vod_time(self, upload_time: float, *, ending: bool = False) -> float:
        """Where ``upload_time`` lands on the VOD. A frame that starts right at a cut lands
        after it; with ``ending``, the time is where the frame before it ends (before the cut)."""
        v = upload_time + self.delay
        for s, e in self.cuts:
            if s < v - EPS or (not ending and s <= v + EPS):
                v += e - s
            else:
                break
        return v

    def part_spans(self) -> list[tuple[float, float]]:
        """(first frame, end of last frame) of each part, in VOD time."""
        spans, u = [], 0.0
        for d in self.parts:
            spans.append((self.vod_time(u), self.vod_time(u + d, ending=True)))
            u += d
        return spans

    def split_intervals(self) -> list[tuple[float, float]]:
        """Where one of these uploads can end: between two parts (across the cut between them,
        if any), or inside a cut before the first part or after the last."""
        spans = self.part_spans()
        if not spans:
            return [(0.0, self.duration)]
        between = [(spans[i][1], spans[i + 1][0]) for i in range(len(spans) - 1)]
        ends = _intersect([(0.0, spans[0][0]), (spans[-1][1], self.duration)], list(self.cuts))
        return sorted(between + ends)


def _intersect(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    for lo1, hi1 in a:
        for lo2, hi2 in b:
            lo, hi = max(lo1, lo2), min(hi1, hi2)
            if hi >= lo - EPS:
                out.append((lo, max(lo, hi)))
    return sorted(out)


# ── Merge ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Side:
    """What a plan needs of one VOD row."""

    id: str
    duration: int  # seconds; vods.duration is whole seconds
    chapters: list
    youtube: list
    drive: list


@dataclass(frozen=True)
class Plan:
    """What to store: the (first) VOD's new fields, plus the numbers behind them."""

    duration: int
    chapters: list
    youtube: list
    drive: list
    detail: dict


def plan_merge(a: Side, b: Side, offset: int) -> Plan:
    """``b`` (the later VOD) appended to ``a`` at ``offset`` seconds of ``a``'s timeline.

    The target keeps its timeline. The source's is shifted by the offset, so a comment or frame
    at source time x lands at offset + x. The gap chapter (restricted, kind "gap") runs from the
    end of the target (where its last uploaded frame lands: its own delay stays at its start) to
    where the source's first uploaded frame lands, offset + the source's delay (and any cuts at
    its start), measured on the played type. The chapters under it are clipped. Then duration −
    Σ parts − Σ cuts equals the target's own delay for that type.
    """
    gap = offset - a.duration
    if gap < 0:
        raise PlanError(f"The VODs overlap: {b.id} starts {offset}s after {a.id}, which is {a.duration}s long "
                        f"(gap {gap}s). Pass a gap if the start times are wrong.",
                        offset=offset, targetDuration=a.duration, gap=gap)
    types = upload_types(a.youtube)
    if types != upload_types(b.youtube):
        raise PlanError(f"{a.id} has {'/'.join(types) or 'no'} uploads and {b.id} has "
                        f"{'/'.join(upload_types(b.youtube)) or 'no'} uploads; the site plays one type back to "
                        "back, so both VODs need uploads of the same types")
    played = played_type(types)
    per_type: dict[str, dict[str, float]] = {}
    for typ in types:
        ta = Timeline.of(a.id, a.duration, a.chapters, a.youtube, typ)
        tb = Timeline.of(b.id, b.duration, b.chapters, b.youtube, typ)
        per_type[typ] = {"targetDelay": ta.delay, "sourceDelay": tb.delay, "sourceFirstFrame": tb.vod_time(0)}

    gap_start = float(a.duration)
    gap_end = offset + (per_type[played]["sourceFirstFrame"] if played else 0.0)
    if gap_end < gap_start - EPS:
        raise PlanError(f"The VODs overlap: the first uploaded frame of {b.id} would land at {gap_end:g}s, before "
                        f"{a.id} ends ({gap_start:g}s). Pass a larger gap if the start times are wrong.",
                        offset=offset, targetDuration=a.duration, gap=gap, sourceFirstFrame=gap_end)
    gap_end = max(gap_end, gap_start)

    shifted_b = shift(b.chapters, offset)
    kept_a, kept_b = clip(a.chapters, -math.inf, gap_start), clip(shifted_b, gap_end, math.inf)
    clipped = sum(c not in kept_a for c in _chapters(a.chapters)) + sum(c not in kept_b for c in shifted_b)
    chapters = kept_a + kept_b
    if gap_end - gap_start > EPS:
        chapters.append(gap_chapter(gap_start, gap_end - gap_start))
    chapters.sort(key=start_of)
    youtube = renumber(a.youtube, b.youtube)
    drive = list(a.drive or []) + [d for d in b.drive or [] if d not in (a.drive or [])]
    duration = offset + b.duration

    for typ, numbers in per_type.items():
        merged = Timeline.of(a.id, duration, chapters, youtube, typ)
        numbers["mergedDelay"] = merged.delay
        # With one gap chapter for every type, only the played type keeps the target's frames exactly
        # in place; another type's are off by the difference in the source's delays.
        numbers["drift"] = merged.delay - numbers["targetDelay"]
    detail = {
        "offset": offset,
        "gap": gap,
        "targetDuration": a.duration,
        "sourceDuration": b.duration,
        "duration": duration,
        "playedType": played,
        "gapChapter": {"start": num_seconds(gap_start), "end": num_seconds(gap_end)} if gap_end - gap_start > EPS
        else None,
        "types": {t: {k: num_seconds(v) for k, v in n.items()} for t, n in per_type.items()},
        "clippedChapters": clipped,  # under the gap chapter: trimmed or dropped
    }
    return Plan(duration, chapters, youtube, drive, detail)


# ── Split ─────────────────────────────────────────────────────────────────


def split_intervals(a: Side) -> list[tuple[float, float]]:
    """Where ``a`` can be split without cutting an upload of any type (a VOD with no uploads: anywhere)."""
    intervals = [(0.0, float(a.duration))]
    for typ in upload_types(a.youtube):
        intervals = _intersect(intervals, Timeline.of(a.id, a.duration, a.chapters, a.youtube, typ).split_intervals())
    return intervals


def nearest_points(intervals: list[tuple[float, float]], at: float, duration: int, count: int = 4) -> list[dict]:
    """The whole-second split points closest to ``at``, one per interval, nearest first."""
    out = []
    for lo, hi in intervals:
        point = round(min(max(at, lo), hi))
        point = min(max(point, math.ceil(lo - SPLIT_SLACK)), math.floor(hi + SPLIT_SLACK))
        if 0 < point < duration:
            out.append({"at": point, "from": num_seconds(lo), "to": num_seconds(hi)})
    out.sort(key=lambda p: (abs(p["at"] - at), p["at"]))
    return out[:count]


def plan_split(a: Side, at: int) -> tuple[Plan, Plan]:
    """The two halves of ``a`` split at ``at`` (whole seconds); the second half's times start at 0."""
    if not 0 < at < a.duration:
        raise PlanError(f"at must be inside the VOD (0 < at < {a.duration})")
    intervals = split_intervals(a)
    if not any(lo - SPLIT_SLACK <= at <= hi + SPLIT_SLACK for lo, hi in intervals):
        raise PlanError(f"{at}s is inside an upload; a VOD can only be split where an upload ends (between two "
                        "parts, or inside a cut)", validPoints=nearest_points(intervals, at, a.duration))

    second_yt: list[dict] = []
    per_type = {}
    for typ in upload_types(a.youtube):
        entries = parts(a.youtube, typ)
        whole = Timeline.of(a.id, a.duration, a.chapters, a.youtube, typ)
        n_first = sum(1 for _, end in whole.part_spans() if end <= at + SPLIT_SLACK)  # spans are in order
        second_yt += entries[n_first:]
        per_type[typ] = {"firstParts": n_first, "secondParts": len(entries) - n_first,
                         "delay": num_seconds(whole.delay)}
    first_yt = [e for e in a.youtube or [] if e not in second_yt]  # the first half keeps its numbering

    # drive entries carry no times, so they all stay on the first half
    first = Plan(at, clip(a.chapters, -math.inf, at), first_yt, list(a.drive or []), {})
    second = Plan(a.duration - at, shift(clip(a.chapters, at, math.inf), -at), renumber(second_yt), [], {})
    for typ, numbers in per_type.items():
        numbers["firstDelay"] = num_seconds(Timeline.of(a.id, first.duration, first.chapters, first.youtube,
                                                        typ).delay)
        numbers["secondDelay"] = num_seconds(Timeline.of(a.id, second.duration, second.chapters, second.youtube,
                                                         typ).delay)
    detail = {
        "at": at,
        "duration": a.duration,
        "validFrom": next(num_seconds(lo) for lo, hi in intervals if lo - SPLIT_SLACK <= at <= hi + SPLIT_SLACK),
        "types": per_type,
    }
    return replace(first, detail=detail), second


# ── Emotes ────────────────────────────────────────────────────────────────

EMOTE_SETS = ("ffz_emotes", "bttv_emotes", "seventv_emotes")  # the emotes row's per-channel columns


def _union(a: Any, b: Any) -> list:
    out, seen = [], set()
    for e in [*(a or []), *(b or [])]:
        key = str(e.get("id")) if isinstance(e, dict) else repr(e)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def union_emotes(a: dict | None, b: dict | None) -> dict | None:
    """Both VODs' emote sets in one, each provider's list without duplicate ids (``a``'s first).
    Takes and returns the emotes columns (EMOTE_SETS + global_emotes*) as a dict."""
    if a is None or b is None:
        return a or b
    out = {k: _union(a.get(k), b.get(k)) for k in EMOTE_SETS}
    ga, gb = a.get("global_emotes"), b.get("global_emotes")
    if ga is None and gb is None:
        out.update(global_emotes=None, global_emotes_source=None, global_emotes_at=None)
    else:
        providers = [*(ga or {}), *(p for p in gb or {} if p not in (ga or {}))]
        out["global_emotes"] = {p: _union((ga or {}).get(p), (gb or {}).get(p)) for p in providers}
        src = a if ga is not None else b
        out["global_emotes_source"], out["global_emotes_at"] = src.get("global_emotes_source"), src.get("global_emotes_at")
    return out
