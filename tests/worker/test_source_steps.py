"""ensure_source -> fetch_vod -> finalize: where a download/reupload/dmca job gets its MP4."""

from __future__ import annotations

import pytest

from archive_worker.context import StepError
from archive_worker.steps import media


@pytest.fixture
def captured(monkeypatch) -> list[bool]:
    calls: list[bool] = []

    async def capture(ctx, *, one_shot=False):
        calls.append(one_shot)

    async def probe(path):
        return 3600.0

    monkeypatch.setattr(media, "capture", capture)
    monkeypatch.setattr(media.ffmpeg, "probe_duration", probe)
    return calls


async def _source_steps(ctx) -> None:
    for step in (media.ensure_source, media.fetch_vod, media.finalize):
        await step(ctx)


async def test_given_path_is_the_source(make_ctx, captured, tmp_path):
    given = tmp_path / "given.mp4"
    given.write_bytes(b"mp4")
    ctx = make_ctx("reupload", "100", {"type": "vod", "path": str(given)})
    await _source_steps(ctx)
    assert captured == []
    assert (ctx.source_mp4, ctx.payload["duration"]) == (given, 3600.0)


async def test_previous_download_is_reused(make_ctx, captured, monkeypatch):
    ctx = make_ctx("download", "100", {"type": "vod"})
    ctx.default_mp4.parent.mkdir(parents=True)
    ctx.default_mp4.write_bytes(b"mp4")
    updates: list[dict] = []

    async def update_vod(**values):
        updates.append(values)

    async def not_spliced():
        pass

    monkeypatch.setattr(ctx, "update_vod", update_vod)
    monkeypatch.setattr(ctx, "refuse_if_spliced", not_spliced)  # its DB check: test_splices.py
    await _source_steps(ctx)
    assert captured == []
    assert ctx.source_mp4 == ctx.default_mp4
    assert updates == [{"duration": "01:00:00"}]


async def test_no_source_downloads_the_vod_once(make_ctx, captured):
    ctx = make_ctx("download", "100", {"type": "vod"})
    await media.ensure_source(ctx)
    await media.fetch_vod(ctx)
    assert captured == [True]  # one-shot; finalize then converts it


async def test_missing_live_recording_fails(make_ctx, captured):
    ctx = make_ctx("reupload", "100", {"type": "live", "stream_id": "555"})
    with pytest.raises(StepError, match="live recording"):
        await media.ensure_source(ctx)
