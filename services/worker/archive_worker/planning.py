"""Pure functions: chapters, part planning, YouTube titles/descriptions, DMCA.

Kept free of I/O so they are easy to unit test.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from archive_common.serialize import box_art_image
from archive_common.timeutil import format_hhmmss

# ── Chapters ──────────────────────────────────────────────────────────────
#
# Stored shape (kept from the legacy API; the frontend depends on it):
#   {gameId, name, image, duration, start, end, restricted}
# where ``duration`` is the chapter *start* as "HH:MM:SS" and ``end`` is the
# chapter *length* in seconds.

# What ``restricted_games`` (and the API's /v1/games-played) calls a chapter with no Twitch category:
# its ``name`` is null, so listing this name is how those chapters get cut.
NO_CATEGORY = "No category"


def chapters_from_moments(
    edges: list[dict[str, Any]], vod_duration: float, restricted_games: list[str]
) -> list[dict[str, Any]]:
    out = []
    for edge in edges:
        node = edge.get("node") or {}
        game = (node.get("details") or {}).get("game")
        pos_ms = node.get("positionMilliseconds") or 0
        dur_ms = node.get("durationMilliseconds") or 0
        start = pos_ms / 1000
        name = game.get("displayName") if game else None
        out.append(
            chapter(
                game.get("id") if game else None,
                name,
                game.get("boxArtURL") if game else None,
                start,
                dur_ms / 1000 if dur_ms else vod_duration - start,
                restricted_game(name, restricted_games),
            )
        )
    return out


def single_chapter(
    game: dict[str, Any] | None, box_art: str | None, vod_duration: float, restricted_games: list[str]
) -> dict[str, Any]:
    name = game.get("displayName") if game else None
    return chapter(
        game.get("id") if game else None,
        name,
        box_art_image(box_art),
        0,
        vod_duration,
        restricted_game(name, restricted_games),
    )


def chapter(
    game_id: str | None, name: str | None, image: str | None, start: float, length: float, restricted: bool
) -> dict[str, Any]:
    """One chapter in the legacy shape (see above: ``duration`` is the start, ``end`` the length)."""
    return {
        "gameId": game_id,
        "name": name,
        "image": image,
        "duration": format_hhmmss(start),
        "start": num_seconds(start),
        "end": num_seconds(length),
        "restricted": restricted,
    }


def num_seconds(v: float) -> int | float:
    v = round(float(v), 3)
    return int(v) if v.is_integer() else v


def restricted_game(name: str | None, restricted_games: list[str]) -> bool:
    """Whether chapters of this game are left out of uploads; ``None`` is a chapter with no category."""
    return (name or NO_CATEGORY) in restricted_games


def is_restricted(chapter: dict[str, Any], restricted_games: list[str]) -> bool:
    return bool(chapter.get("restricted")) or restricted_game(chapter.get("name"), restricted_games)


# ── Live recordings ───────────────────────────────────────────────────────
#
# A live recording is not on the VOD's timeline: it starts after the stream does, and ad breaks,
# reconnects and restarts leave holes in it. Its ``timeline`` maps it to wall-clock time, from the
# segments' EXT-X-PROGRAM-DATE-TIME: [recording start, wall-clock start (epoch s), length] for each
# stretch recorded without a gap. The VOD's own time is wall-clock time since ``vods.created_at``.

TIMELINE_TOLERANCE = 1.0  # seconds of clock jitter between segments that are not a gap


def recording_timeline(segments: list[tuple[float, float | None]]) -> list[list[float]]:
    """(duration, wall-clock start or None) of each recorded segment, in order -> the spans.
    An untimed segment continues the span before it; none timed at all (old recordings) gives []."""
    spans: list[list[float]] = []
    rec = 0.0
    for duration, wall in segments:
        if spans and (wall is None or abs(wall - (spans[-1][1] + spans[-1][2])) <= TIMELINE_TOLERANCE):
            spans[-1][2] += duration
        elif wall is not None:
            spans.append([rec, wall, duration])
        rec += duration
    return [[round(r, 3), round(w, 3), round(n, 3)] for r, w, n in spans]


def recording_time(vod_seconds: float, vod_start: float, timeline: list[list[float]]) -> float:
    """Where ``vod_seconds`` into the VOD is in the recording. A moment that was not recorded
    maps to the next one that was (the recording's end, past the last)."""
    wall = vod_start + vod_seconds
    for rec, start, length in timeline:
        if wall < start:
            return rec
        if wall <= start + length:
            return rec + (wall - start)
    rec, _, length = timeline[-1]
    return rec + length


def chapters_on_recording(
    chapters: list[dict[str, Any]] | None, vod_start: float, timeline: list[list[float]]
) -> list[dict[str, Any]]:
    """The VOD's chapters moved onto a live recording; one that was not recorded at all is left out."""
    out = []
    for ch in chapters or []:
        s = float(ch.get("start") or 0)
        a = recording_time(s, vod_start, timeline)
        b = recording_time(s + float(ch.get("end") or 0), vod_start, timeline)
        if b > a:
            out.append(ch | {"duration": format_hhmmss(a), "start": num_seconds(a), "end": num_seconds(b - a)})
    return out


def recording_gaps(
    duration: float,
    vod_start: float,
    timeline: list[list[float]],
    cuts: list[tuple[float, float]],
    part_lengths: list[float],
) -> list[list[list[float]]]:
    """Each part's ``gaps``: [seconds into the part, VOD seconds missing there] for every stretch of
    the VOD the recording missed after its first frame (ad breaks, reconnects, an early end), less
    the cuts (restricted chapters, already out of every upload). What is missing before the first
    frame stays the timeline's ``delay``. Times are VOD seconds; ``cuts`` are (start, end)."""
    gaps: list[list[list[float]]] = [[] for _ in part_lengths]
    if not timeline or not part_lengths:
        return gaps
    covered = [(w - vod_start, w - vod_start + n) for _, w, n in timeline]
    holes, t = [], covered[0][1]
    for s, e in covered[1:]:
        if s - t > TIMELINE_TOLERANCE:
            holes.append((t, s))
        t = max(t, e)
    if duration - t > TIMELINE_TOLERANCE:
        holes.append((t, float(duration)))
    holes = _subtract(holes, cuts)
    missing = _union([(0.0, covered[0][0]), *cuts, *holes])
    starts = [sum(part_lengths[:i]) for i in range(len(part_lengths))]
    for s, e in holes:
        u = s - sum(max(0.0, min(b, s) - a) for a, b in missing)  # upload time where the hole starts
        i = next((i for i in range(len(starts)) if u <= starts[i] + part_lengths[i] + 0.001), len(starts) - 1)
        at = min(max(0.0, u - starts[i]), part_lengths[i])
        gaps[i].append([num_seconds(at), num_seconds(e - s)])
    return gaps


def _union(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(x for x in spans if x[1] > x[0]):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _subtract(spans: list[tuple[float, float]], minus: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    for s, e in spans:
        pieces = [(s, e)]
        for a, b in minus:
            pieces = [p for lo, hi in pieces for p in ((lo, min(hi, a)), (max(lo, b), hi)) if p[1] - p[0] > 0.001]
        out += [p for p in pieces if p[1] - p[0] > TIMELINE_TOLERANCE]
    return out


# ── Parts ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Part:
    number: int  # 1-based
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def part_gaps(
    vod_seconds: float,
    vod_start: float,
    chapters: list[dict[str, Any]] | None,
    restricted_games: list[str],
    timeline: list[list[float]],
    parts: list[Part],
) -> dict[int, list[list[float]]]:
    """Part number -> its upload's ``gaps`` (recording_gaps), for parts cut from a live recording.
    ``parts`` are every part of the recording, in order; a part without gaps is left out."""
    if not vod_seconds or not timeline or not parts:
        return {}
    cuts = [
        (float(c.get("start") or 0), float(c.get("start") or 0) + float(c.get("end") or 0))
        for c in chapters or []
        if is_restricted(c, restricted_games)
    ]
    gaps = recording_gaps(vod_seconds, vod_start, timeline, cuts, [p.duration for p in parts])
    return {p.number: g for p, g in zip(parts, gaps, strict=True) if g}


def plan_parts(
    duration: float,
    chapters: list[dict[str, Any]] | None,
    restricted_games: list[str],
    split_duration: int,
    min_part: float = 1.0,
) -> list[Part]:
    """Split [0, duration) into parts of at most ``split_duration`` seconds,
    skipping restricted-game chapters entirely (a part never spans one)."""
    blocked: list[tuple[float, float]] = []
    for ch in chapters or []:
        if is_restricted(ch, restricted_games):
            s = float(ch.get("start") or 0)
            blocked.append((s, s + float(ch.get("end") or 0)))
    blocked.sort()

    allowed: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in blocked:
        if s > cursor:
            allowed.append((cursor, min(s, duration)))
        cursor = max(cursor, e)
    if cursor < duration:
        allowed.append((cursor, duration))

    parts: list[Part] = []
    for a, b in allowed:
        t = a
        while b - t >= min_part:
            e = min(t + split_duration, b)
            parts.append(Part(len(parts) + 1, t, e))
            t = e
    return parts


def select_parts(parts: list[Part], start_part: int | None, end_part: int | None) -> list[Part]:
    """``start_part``/``end_part`` are 1-based and inclusive."""
    lo = start_part or 1
    hi = end_part or len(parts)
    return [p for p in parts if lo <= p.number <= hi]


# ── YouTube metadata ──────────────────────────────────────────────────────


def local_date(created_at: dt.datetime, tz: str) -> str:
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.UTC)
    return created_at.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


def video_title(channel: str, kind: str, created_at: dt.datetime, tz: str, part: int, total_parts: int) -> str:
    label = "Live VOD" if kind == "live" else "VOD"
    title = f"{channel} Twitch {label} - {local_date(created_at, tz)}"
    if total_parts > 1:
        title += f" PART {part}"
    return title[:100]


def base_description(domain: str, vod_id: str, stream_title: str | None, extra: str) -> str:
    clean = (stream_title or "").replace("<", "").replace(">", "")
    return f"Chat Replay: https://{domain}/youtube/{vod_id}\nStream Title: {clean}\n{extra}"


def chapter_lines(chapters: list[dict[str, Any]] | None, part: Part, restricted_games: list[str]) -> list[str]:
    lines = []
    for ch in chapters or []:
        if is_restricted(ch, restricted_games):
            continue
        s = float(ch.get("start") or 0)
        e = s + float(ch.get("end") or 0)
        if s < part.end and e > part.start:
            lines.append(f"{format_hhmmss(max(0.0, s - part.start))} {ch.get('name') or 'Unknown'}")
    return lines


def full_description(
    base: str,
    this_part: int,
    siblings: list[dict[str, Any]],
    chapters: list[str],
) -> str:
    """Rebuilt from scratch every time, so re-running describe is idempotent."""
    part_lines = [
        f"PART {s['part']}: https://youtube.com/watch?v={s['id']}"
        for s in sorted(siblings, key=lambda s: s.get("part") or 0)
        if s.get("part") != this_part
    ]
    text = ""
    if part_lines:
        text += "\n".join(part_lines) + "\n\n"
    text += base
    if chapters:
        text += "\n\n" + "\n".join(chapters)
    return text[:5000]


def privacy(kind: str, public: bool, live_record: bool) -> str:
    """``public`` applies to the main copy only: the live copy when live_record is on, the VOD
    copy otherwise. multi_track only adds the VOD copy as an unlisted second upload."""
    main = "live" if live_record else "vod"
    return "public" if public and kind == main else "unlisted"


def youtube_thumbnail(video_id: str) -> str:
    """YouTube's default thumbnail, for when the API gave none."""
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def upsert_youtube_entry(entries: list[dict[str, Any]] | None, entry: dict[str, Any]) -> list[dict]:  # type: ignore[type-arg]
    out = [e for e in (entries or []) if not (e.get("type") == entry["type"] and e.get("part") == entry["part"])]
    out.append(entry)
    out.sort(key=lambda e: (e.get("type") != "vod", e.get("part") or 0))
    return out


# ── DMCA ──────────────────────────────────────────────────────────────────

BLOCKING_POLICIES = {"POLICY_TYPE_GLOBAL_BLOCK", "POLICY_TYPE_MOSTLY_GLOBAL_BLOCK", "POLICY_TYPE_BLOCK"}


@dataclass(frozen=True)
class DmcaPlan:
    mute: list[tuple[float, float]]
    blackout: list[tuple[float, float]]

    @property
    def empty(self) -> bool:
        return not self.mute and not self.blackout


def plan_dmca(claims: list[dict[str, Any]]) -> DmcaPlan:
    """Turn YouTube Studio claim objects into mute / blackout ranges."""
    mute: list[tuple[float, float]] = []
    black: list[tuple[float, float]] = []
    for claim in claims:
        policy = ((claim.get("claimPolicy") or {}).get("primaryPolicy") or {}).get("policyType")
        if policy not in BLOCKING_POLICIES:
            continue
        details = claim.get("matchDetails") or {}
        start = float(details.get("longestMatchStartTimeSeconds") or 0)
        length = float(details.get("longestMatchDurationSeconds") or 0)
        if length <= 0:
            continue
        rng = (start, start + length)
        kind = claim.get("type")
        if kind in ("CLAIM_TYPE_AUDIO", "CLAIM_TYPE_AUDIOVISUAL"):
            mute.append(rng)
        if kind in ("CLAIM_TYPE_VISUAL", "CLAIM_TYPE_AUDIOVISUAL"):
            black.append(rng)
    return DmcaPlan(_merge(mute), _merge(black))


def _merge(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


# ── Censor (vods.censor) ──────────────────────────────────────────────────

COVER_RANK = {"blur": 1, "black": 2}  # where ranges overlap, black wins


@dataclass(frozen=True)
class CensorPlan:
    cover: list[tuple[float, float, str]]  # (start, end, "blur" | "black"), sorted, not overlapping
    mute: list[tuple[float, float]]

    @property
    def empty(self) -> bool:
        return not self.cover and not self.mute


def plan_censor(ranges: list[dict[str, Any]] | None) -> CensorPlan:
    """``vods.censor`` ranges (on the file's timeline) -> what to cover and what to mute."""
    marks: set[float] = set()
    covers: list[tuple[float, float, int]] = []
    mute: list[tuple[float, float]] = []
    for r in ranges or []:
        a, b = float(r["start"]), float(r["end"])
        if b <= a:
            continue
        if r.get("video") in COVER_RANK:
            covers.append((a, b, COVER_RANK[r["video"]]))
            marks |= {a, b}
        if r.get("mute"):
            mute.append((a, b))
    names = {v: k for k, v in COVER_RANK.items()}
    cover: list[tuple[float, float, str]] = []
    points = sorted(marks)
    for a, b in zip(points, points[1:], strict=False):
        rank = max((c[2] for c in covers if c[0] <= a and b <= c[1]), default=0)
        if not rank:
            continue
        if cover and cover[-1][1] == a and cover[-1][2] == names[rank]:
            cover[-1] = (cover[-1][0], b, names[rank])
        else:
            cover.append((a, b, names[rank]))
    return CensorPlan(cover, _merge(mute))


def censor_on_recording(
    ranges: list[dict[str, Any]] | None, vod_start: float, timeline: list[list[float]]
) -> list[dict[str, Any]]:
    """The VOD's censor ranges moved onto a live recording; one that was not recorded at all is left out."""
    out = []
    for r in ranges or []:
        a = recording_time(float(r["start"]), vod_start, timeline)
        b = recording_time(float(r["end"]), vod_start, timeline)
        if b > a:
            out.append(r | {"start": num_seconds(a), "end": num_seconds(b)})
    return out
