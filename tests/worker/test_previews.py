"""Seek-bar previews: sheet layout, the previews / previews_fetch steps, admin edits and the API route."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete

from archive_common import previews as pv
from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from archive_worker import ffmpeg, vod_edits
from archive_worker.context import StepError
from archive_worker.steps import previews as step

VOD, HIDDEN = "test-previews-1", "test-previews-2"
YT, YT2, YT_HIDDEN = "abcdefghijk", "bcdefghijkl", "cdefghijklm"
START = dt.datetime(2001, 4, 5, 20, 0, tzinfo=dt.timezone.utc)


# ── Layout ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("duration", "frames", "sheets"), [
    (0, 1, 1), (9.9, 1, 1), (10, 1, 1), (10.1, 2, 1), (1000, 100, 1), (1000.5, 101, 2), (1249.97, 125, 2),
    (10800, 1080, 11), (-5, 1, 1),
])
def test_frame_and_sheet_counts(duration, frames, sheets):
    assert pv.frame_count(duration) == frames
    assert pv.sheet_count(frames) == sheets


def test_info_and_directory(tmp_path):
    assert pv.info(7) == {"v": 1, "interval": 10, "w": 160, "h": 90, "cols": 10, "rows": 10, "count": 7}
    assert pv.directory(tmp_path, YT) == tmp_path / YT
    for bad in ("short", "../../etc/pa", "abcdefghij/", "abcdefghijkl"):
        with pytest.raises(ValueError):
            pv.directory(tmp_path, bad)


# ── Admin edits ───────────────────────────────────────────────────────────


def test_admin_youtube_edit_keeps_previews_of_kept_videos():
    preview = pv.info(3)
    existing = [{"id": YT, "type": "vod", "duration": 25, "part": 1, "thumbnail_url": "t", "preview": preview}]
    out = vod_edits.youtube([{"id": YT, "type": "vod", "part": 2}, {"id": YT2, "type": "vod", "part": 1}], existing)
    assert out[0]["preview"] == preview and out[0]["part"] == 2
    assert "preview" not in out[1]  # a new video has none until the worker makes them


# ── Steps (on the dev DB; ffmpeg and yt-dlp faked) ────────────────────────


async def _clean():
    async with get_sessionmaker()() as s:
        await s.execute(delete(Vod).where(Vod.id.in_((VOD, HIDDEN))))
        await s.commit()


@pytest.fixture
async def vods(db):
    await _clean()
    async with get_sessionmaker()() as s:
        s.add_all([
            Vod(id=VOD, title="t", created_at=START, duration="00:20:00", youtube=[
                {"id": YT, "type": "vod", "part": 1, "duration": 600},
                {"id": YT2, "type": "vod", "part": 2, "duration": 600},
            ]),
            Vod(id=HIDDEN, title="h", created_at=START, duration="00:10:00", hidden=True,
                youtube=[{"id": YT_HIDDEN, "type": "vod", "part": 1, "duration": 600}]),
        ])
        await s.commit()
    yield
    await _clean()


async def _youtube(vod_id=VOD) -> list[dict]:
    async with get_sessionmaker()() as s:
        return (await s.get(Vod, vod_id)).youtube


@pytest.fixture
def fake_sheets(monkeypatch):
    """ffmpeg.preview_sheets writing one empty sheet; the sources it was given, in order."""
    made: list[Path] = []

    async def sheets(src: Path, out_dir: Path) -> int:
        if "broken" in src.name:
            raise ffmpeg.FfmpegError("broken")
        made.append(src)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "0.jpg").write_bytes(b"jpg")
        return 61

    monkeypatch.setattr(ffmpeg, "preview_sheets", sheets)
    return made


async def test_previews_step_does_the_uploaded_parts_once(vods, make_ctx, settings, fake_sheets):
    parts = [{"number": 1, "path": "/w/p1.mp4"}, {"number": 2, "path": "/w/p2-broken.mp4"},
             {"number": 3, "path": "/w/p3.mp4"}]  # part 3 was not uploaded
    uploaded = {"1": {"id": YT}, "2": {"id": YT2}}
    await step.previews(make_ctx(vod_id=VOD, payload={"parts": parts, "uploaded": uploaded}))
    assert fake_sheets == [Path("/w/p1.mp4")]
    first, second = await _youtube()
    assert first["preview"] == pv.info(61) and "preview" not in second  # a failed part never fails the job
    assert (settings.previews_dir / YT / "0.jpg").exists()

    await step.previews(make_ctx(vod_id=VOD, payload={"parts": parts, "uploaded": uploaded}))
    assert fake_sheets == [Path("/w/p1.mp4")]  # done already: skipped on a retry


async def test_previews_step_off(vods, make_ctx, settings, fake_sheets):
    settings.previews = False
    await step.previews(make_ctx(vod_id=VOD, payload={"parts": [{"number": 1, "path": "/p"}], "uploaded": {"1": {"id": YT}}}))
    assert fake_sheets == []


async def test_previews_fetch_downloads_and_deletes(vods, make_ctx, settings, fake_sheets, monkeypatch):
    settings.previews_fetch_pause_seconds = 0
    got: list[str] = []

    async def download(ctx, youtube_id, into):
        got.append(youtube_id)
        if youtube_id == YT2:
            raise StepError("bot check")
        into.mkdir(parents=True, exist_ok=True)
        path = into / f"{youtube_id}.webm"
        path.write_bytes(b"video")
        return path

    monkeypatch.setattr(step, "_download", download)
    ctx = make_ctx("previews_fetch", vod_id=VOD, job_id=987654)
    await step.previews_fetch(ctx)
    assert got == [YT, YT2]
    assert [e.get("preview") for e in await _youtube()] == [pv.info(61), None]
    assert not ctx.work_dir.exists()  # downloads deleted

    got.clear()
    with pytest.raises(StepError, match="no previews made"):  # every remaining upload failed: retried later
        await step.previews_fetch(ctx)
    assert got == [YT2]


# ── API ───────────────────────────────────────────────────────────────────


@pytest.fixture
def preview_api(settings):
    from archive_api.main import create_app

    settings.rate_limit_points = 1  # previews are not rate limited
    app = create_app(settings)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")


async def test_api_serves_sheets_of_shown_vods_only(vods, settings, preview_api):
    for yid in (YT, YT_HIDDEN, "ddddddddddd"):
        (settings.previews_dir / yid).mkdir(parents=True)
        (settings.previews_dir / yid / "0.jpg").write_bytes(b"jpg")
    async with preview_api as api:
        for _ in range(3):
            r = await api.get(f"/v1/previews/{YT}/0.jpg")
            assert r.status_code == 200 and r.content == b"jpg"
        assert r.headers["content-type"] == "image/jpeg"
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
        for path in (f"/v1/previews/{YT}/1.jpg",  # no such sheet
                     f"/v1/previews/{YT_HIDDEN}/0.jpg",  # hidden VOD
                     "/v1/previews/ddddddddddd/0.jpg",  # on no VOD
                     f"/v1/previews/{YT}/0.png", f"/v1/previews/{YT}/..%2F0.jpg", "/v1/previews/short/0.jpg"):
            assert (await api.get(path)).status_code == 404, path
