import datetime as dt

import pytest
from archive_common.db import get_sessionmaker
from archive_common.models import AppState
from archive_worker import youtube_quota
from sqlalchemy import delete

NOON_UTC = dt.datetime(2026, 10, 13, 12, tzinfo=dt.UTC)  # 05:00 in Los Angeles (PDT)


def test_the_day_and_its_reset_follow_pacific_time():
    assert youtube_quota.day(NOON_UTC) == "2026-10-13"
    assert youtube_quota.day(dt.datetime(2026, 10, 13, 3, tzinfo=dt.UTC)) == "2026-10-12"
    assert youtube_quota.resets_at(NOON_UTC) == dt.datetime(2026, 10, 14, 7, tzinfo=dt.UTC)
    assert youtube_quota.resets_at(dt.datetime(2026, 12, 1, tzinfo=dt.UTC)) == dt.datetime(
        2026, 12, 1, 8, tzinfo=dt.UTC
    )


def test_add_counts_units_and_calls_and_starts_again_each_day():
    v = youtube_quota.add(None, "videos.insert", "2026-10-13")
    v = youtube_quota.add(v, "videos.list", "2026-10-13")
    assert v == {"day": "2026-10-13", "used": 1601, "calls": {"videos.insert": 1, "videos.list": 1}}
    assert youtube_quota.add(v, "videos.update", "2026-10-14") == {
        "day": "2026-10-14",
        "used": 50,
        "calls": {"videos.update": 1},
    }


@pytest.fixture
async def clean():
    async def wipe():
        async with get_sessionmaker()() as s:
            await s.execute(delete(AppState).where(AppState.key == youtube_quota.STATE_KEY))
            await s.commit()

    await wipe()
    yield
    await wipe()


async def test_spend_and_today_share_the_count(clean):
    assert (await youtube_quota.today(10_000))["used"] == 0
    await youtube_quota.spend("videos.insert")
    await youtube_quota.spend("channels.list")
    now = await youtube_quota.today(10_000)
    assert (now["used"], now["limit"], now["calls"]) == (1601, 10_000, {"videos.insert": 1, "channels.list": 1})
    assert now["resetsAt"].endswith(("Z", "+00:00"))
    tomorrow = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    assert (await youtube_quota.today(10_000, tomorrow))["used"] == 0


async def test_spend_never_raises(clean, monkeypatch):
    def broken():
        raise RuntimeError("no database")

    monkeypatch.setattr(youtube_quota, "get_sessionmaker", broken)
    await youtube_quota.spend("videos.insert")
