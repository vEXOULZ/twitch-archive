"""Whether each VOD's uploads are still on YouTube, and how much of the VOD they hold.

One ``videos.list`` (1 unit) reads the status and length of 50 uploads, so a check of the whole
archive costs a few units. For each VOD with uploads it reports every copy (``vod``, ``live``):
its parts, the seconds they hold, the ad breaks a live recording missed, and the share of the VOD
that is. The planned length is what the uploads were meant to hold: the VOD less the chapters of
restricted games, under today's settings. Problems per upload: gone from YouTube, rejected or
failed, still processing, blocked in some countries, age-restricted, or shorter than what was
uploaded (trimmed in YouTube Studio, say for a claim); and a part number missing between two.

The last check is kept in one ``app_state`` row (key ``youtube_check``) with only the VODs that
have a problem; ``run_forever`` checks every ``youtube_check_hours``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from archive_common.config import Settings
from archive_common.db import get_sessionmaker
from archive_common.models import AppState, Vod
from archive_common.timeutil import format_hhmmss, hhmmss_to_seconds
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from . import planning
from .events import iso_utc

log = logging.getLogger(__name__)

STATE_KEY = "youtube_check"
BATCH = 50  # ids per videos.list
# An upload this much shorter on YouTube than when it was uploaded was cut there.
SHORT_BY = 5.0
# A copy holding this much less than planned, ad breaks counted, is incomplete.
INCOMPLETE_BY = 60.0
BAD = "bad"
WARN = "warn"
REJECTIONS = {
    "claim": "a claim",
    "copyright": "copyright",
    "duplicate": "a duplicate",
    "inappropriate": "inappropriate content",
    "legal": "legal reasons",
    "length": "too long",
    "termsOfUse": "terms of use",
    "trademark": "trademark",
    "uploaderAccountClosed": "the account was closed",
    "uploaderAccountSuspended": "the account was suspended",
}

Videos = Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]]

_ISO = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def iso_seconds(value: str | None) -> float | None:
    """``PT1H2M3S`` (YouTube's contentDetails.duration) in seconds; None when unreadable."""
    m = _ISO.match(value or "")
    if not m:
        return None
    d, h, mi, s = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def _issue(kind: str, severity: str, detail: str) -> dict[str, Any]:
    return {"kind": kind, "severity": severity, "detail": detail}


def video_issues(entry: dict[str, Any], video: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What is wrong with one upload (an entry of ``vods.youtube``), from its videos.list resource
    (None: YouTube didn't return it)."""
    gone = "Not on YouTube any more: deleted, or not visible to the connected channel"
    if video is None:
        return [_issue("removed", BAD, gone)]
    status = video.get("status") or {}
    details = video.get("contentDetails") or {}
    out: list[dict[str, Any]] = []
    upload = status.get("uploadStatus")
    if upload == "deleted":
        return [_issue("removed", BAD, gone)]
    if upload == "rejected":
        why = status.get("rejectionReason") or ""
        out.append(_issue("rejected", BAD, f"Rejected by YouTube: {REJECTIONS.get(why, why or 'no reason given')}"))
    elif upload == "failed":
        out.append(
            _issue("failed", BAD, f"Upload failed on YouTube: {status.get('failureReason') or 'no reason given'}")
        )
    elif upload == "uploaded":
        out.append(_issue("processing", WARN, "Still processing on YouTube"))
    region = details.get("regionRestriction") or {}
    if region.get("blocked"):
        n = len(region["blocked"])
        out.append(_issue("blocked", WARN, f"Blocked in {n} {'country' if n == 1 else 'countries'}"))
    if "allowed" in region:
        n = len(region.get("allowed") or [])
        out.append(_issue("blocked", WARN, f"Only viewable in {n} {'country' if n == 1 else 'countries'}"))
    if (details.get("contentRating") or {}).get("ytRating") == "ytAgeRestricted":
        out.append(_issue("ageRestricted", WARN, "Age-restricted"))
    there, ours = iso_seconds(details.get("duration")), _num(entry.get("duration"))
    if upload == "processed" and there and ours and there < ours - SHORT_BY:
        out.append(
            _issue("shorter", WARN, f"{format_hhmmss(there)} on YouTube of {format_hhmmss(ours)} uploaded: cut there")
        )
    return out


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _gap_seconds(entry: dict[str, Any]) -> float:
    return sum(max(0.0, float(g[1]) - float(g[0])) for g in entry.get("gaps") or [] if len(g) >= 2)


def vod_report(
    vod_id: str,
    title: str | None,
    seconds: float,
    planned: float,
    entries: Iterable[dict[str, Any]],
    videos: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """One VOD: each copy's parts and how much they hold, its best share of the VOD, and the problems."""
    copies: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    by_type: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        if isinstance(e, dict) and e.get("id"):
            by_type.setdefault(str(e.get("type") or "vod"), []).append(e)
    for kind, parts in sorted(by_type.items(), key=lambda kv: kv[0] != "vod"):
        parts.sort(key=lambda e: int(e.get("part") or 1))
        held = gaps = 0.0
        for e in parts:
            video = videos.get(str(e["id"]))
            found = video_issues(e, video)
            issues += [{**i, "type": kind, "part": int(e.get("part") or 1), "video": e["id"]} for i in found]
            if any(i["severity"] == BAD for i in found):
                continue
            there = iso_seconds(((video or {}).get("contentDetails") or {}).get("duration"))
            held += there or _num(e.get("duration")) or 0.0
            gaps += _gap_seconds(e)
        numbers = {int(e.get("part") or 1) for e in parts}
        for n in sorted(set(range(1, max(numbers) + 1)) - numbers):
            issues.append({**_issue("missingPart", BAD, f"No part {n}"), "type": kind, "part": n, "video": None})
        share = min(1.0, held / seconds) if seconds else 0.0
        copies.append({"type": kind, "parts": len(parts), "seconds": round(held), "gaps": round(gaps), "share": share})
    best = max(copies, key=lambda c: c["seconds"] + c["gaps"], default=None)
    if best and planned and best["seconds"] + best["gaps"] < planned - INCOMPLETE_BY:
        short = format_hhmmss(planned - best["seconds"] - best["gaps"])
        issues.append(
            {**_issue("incomplete", BAD, f"{short} of the planned uploads isn't on YouTube"), "type": best["type"]}
        )
    return {
        "id": vod_id,
        "title": title,
        "seconds": round(seconds),
        "planned": round(planned),
        "share": best["share"] if best else 0.0,
        "copies": copies,
        "issues": issues,
    }


async def _vods() -> list[Vod]:
    """VODs with uploads; a synthetic VOD has none of its own."""
    async with get_sessionmaker()() as s:
        rows = await s.execute(
            select(Vod)
            .where(Vod.synthetic.is_(None), func.coalesce(func.jsonb_array_length(Vod.youtube), 0) > 0)
            .order_by(Vod.created_at.desc())
        )
        return list(rows.scalars())


async def check(videos: Videos, settings: Settings, now: dt.datetime | None = None) -> dict[str, Any]:
    """Check every upload, keep the result and return it."""
    vods = await _vods()
    ids = sorted({str(e["id"]) for v in vods for e in v.youtube or [] if isinstance(e, dict) and e.get("id")})
    found: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), BATCH):
        found |= await videos(ids[i : i + BATCH])
    reports = []
    for v in vods:
        seconds = float(hhmmss_to_seconds(v.duration) or 0)
        plan = planning.plan_parts(seconds, v.chapters, settings.restricted_games, settings.split_duration)
        reports.append(vod_report(v.id, v.title, seconds, sum(p.duration for p in plan), v.youtube or [], found))
    problems = [r for r in reports if r["issues"]]
    result = {
        "checkedAt": iso_utc(now or dt.datetime.now(dt.UTC)),
        "videos": len(ids),
        "vods": len(reports),
        "whole": sum(1 for r in reports if not any(i["severity"] == BAD for i in r["issues"])),
        "bad": sum(1 for r in problems if any(i["severity"] == BAD for i in r["issues"])),
        "warn": sum(1 for r in problems if all(i["severity"] == WARN for i in r["issues"])),
        "seconds": sum(r["seconds"] for r in reports),
        "onYoutube": round(sum(r["share"] * r["seconds"] for r in reports)),
        "problems": problems,
    }
    async with get_sessionmaker()() as s:
        stmt = insert(AppState).values(key=STATE_KEY, value=result)
        await s.execute(stmt.on_conflict_do_update(index_elements=[AppState.key], set_={"value": stmt.excluded.value}))
        await s.commit()
    return result


async def last() -> dict[str, Any] | None:
    async with get_sessionmaker()() as s:
        row = await s.get(AppState, STATE_KEY)
    return row.value if row and row.value else None


class Checker:
    """One check at a time, from the dashboard or the timer."""

    def __init__(self, videos: Videos, settings: Settings) -> None:
        self.videos = videos
        self.settings = settings
        self.lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.lock.locked()

    async def run(self) -> dict[str, Any]:
        async with self.lock:
            return await check(self.videos, self.settings)

    async def run_forever(self, authorized: Callable[[], Awaitable[bool]]) -> None:
        """Every ``youtube_check_hours`` (0 = off, looked at hourly) while YouTube is connected."""
        while True:
            hours = self.settings.youtube_check_hours
            if not hours:
                await asyncio.sleep(3600)
                continue
            prev = await last()
            since = None
            if prev and prev.get("checkedAt"):
                since = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(prev["checkedAt"].replace("Z", "+00:00"))
            if since is None or since >= dt.timedelta(hours=hours):
                try:
                    if await authorized():
                        result = await self.run()
                        log.info("YouTube check: %d videos, %d VODs with problems", result["videos"], result["bad"])
                except Exception:
                    log.exception("YouTube check failed")
            await asyncio.sleep(min(3600, hours * 3600))
