"""The YouTube Data API units the worker spent today, for the dashboard.

Google gives a project a daily quota (10,000 units by default) and no way to ask how much is
left, so the worker counts what it spends itself: each call adds its cost (``COST``) to one
``app_state`` row (key ``youtube_quota``), which starts again when the Pacific day turns over,
as Google's count does. Calls made outside the worker (the YouTube Studio, another app on the
same project) aren't in it.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

from archive_common.db import get_sessionmaker
from archive_common.models import AppState
from sqlalchemy.dialects.postgresql import insert

from .events import iso_utc

log = logging.getLogger(__name__)

STATE_KEY = "youtube_quota"
# Units per call, from Google's quota cost table (developers.google.com/youtube/v3/determine_quota_cost).
COST = {"videos.insert": 1600, "videos.update": 50, "videos.delete": 50, "videos.list": 1, "channels.list": 1}
PACIFIC = ZoneInfo("America/Los_Angeles")  # Google's daily quota resets at midnight Pacific


def day(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.UTC)).astimezone(PACIFIC).date().isoformat()


def resets_at(now: dt.datetime | None = None) -> dt.datetime:
    """The next Pacific midnight, in UTC."""
    local = (now or dt.datetime.now(dt.UTC)).astimezone(PACIFIC)
    midnight = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(), PACIFIC)
    return midnight.astimezone(dt.UTC)


def add(value: dict[str, Any] | None, call: str, today: str) -> dict[str, Any]:
    """``value`` with one ``call`` counted; a count from an earlier day starts again at 0."""
    if not value or value.get("day") != today:
        value = {"day": today, "used": 0, "calls": {}}
    calls = dict(value.get("calls") or {})
    calls[call] = calls.get(call, 0) + 1
    return {"day": today, "used": int(value.get("used") or 0) + COST[call], "calls": calls}


async def spend(call: str) -> None:
    """Count one ``call`` (a key of ``COST``). Never raises: a lost count mustn't fail an upload."""
    try:
        today = day()
        async with get_sessionmaker()() as s:
            await s.execute(insert(AppState).values(key=STATE_KEY, value={}).on_conflict_do_nothing())
            row = await s.get(AppState, STATE_KEY, with_for_update=True, populate_existing=True)
            assert row is not None
            row.value = add(row.value, call, today)
            await s.commit()
    except Exception:
        log.exception("Could not count the YouTube quota for %s", call)


async def today(limit: int, now: dt.datetime | None = None) -> dict[str, Any]:
    """``{used, limit, resetsAt, calls}`` for the current Pacific day."""
    async with get_sessionmaker()() as s:
        row = await s.get(AppState, STATE_KEY)
    value = row.value if row else None
    if not value or value.get("day") != day(now):
        value = {"used": 0, "calls": {}}
    return {
        "used": int(value.get("used") or 0),
        "limit": limit,
        "resetsAt": iso_utc(resets_at(now)),
        "calls": value.get("calls") or {},
    }
