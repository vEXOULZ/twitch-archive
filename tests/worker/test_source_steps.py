"""ensure_source -> fetch_vod -> finalize: where a download/reupload/dmca job gets its MP4."""

from __future__ import annotations

from typing import Any

import pytest
from archive_worker.context import StepError
from archive_worker.steps import media
from pydantic import SecretStr


@pytest.fixture
def captured(monkeypatch) -> list[bool]:
    calls: list[bool] = []

    async def capture(ctx, *, one_shot=False):
        calls.append(one_shot)

    async def probe(path):
        return 3600.0

    monkeypatch.setattr(media, "capture", capture)
    monkeypatch.setattr(media.ffmpeg, "probe_duration", probe)  # type: ignore[attr-defined]
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
    updates: list[dict[str, Any]] = []

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


@pytest.mark.parametrize(("archive_job", "saved"), [(False, ["03:02:01"]), (True, [])])
async def test_resolve_vod_saves_the_final_duration_without_an_archive_job(
    make_ctx, settings, monkeypatch, archive_job, saved
):
    from archive_worker import jobs

    settings.twitch_client_id, settings.twitch_client_secret = "id", SecretStr("secret")
    ctx = make_ctx("live", None, {"type": "live", "stream_id": "42"})
    durations: list[str] = []

    async def vod_id_for_stream(_stream_id: str) -> str:
        return "100"

    async def video_for_stream(_user_id: str, _stream_id: str) -> dict[str, Any]:
        return {"id": "100", "duration": "3h2m1s"}

    async def exists_any(kind: str, stream_id: str) -> bool:
        return bool(archive_job)

    async def update_vod(**values: Any) -> None:
        durations.append(values["duration"])

    monkeypatch.setattr(media, "vod_id_for_stream", vod_id_for_stream)
    monkeypatch.setattr(ctx.deps.helix, "video_for_stream", video_for_stream)
    monkeypatch.setattr(jobs, "exists_any", exists_any)
    monkeypatch.setattr(ctx, "update_vod", update_vod)
    await media.resolve_vod(ctx)
    assert ctx.vod_id == "100"
    assert durations == saved


async def test_a_live_jobs_chapters_use_the_vods_length(make_ctx):
    from archive_common.models import Vod
    from archive_worker.steps.metadata import twitch_vod_duration

    vod = Vod(id="100", duration="02:00:00")
    assert await twitch_vod_duration(make_ctx("live", "100", {"type": "live", "duration": 7000.0}), vod) == 7200.0
    assert await twitch_vod_duration(make_ctx("archive", "100", {"type": "vod", "duration": 7000.0}), vod) == 7000.0
    vod.duration = "00:00:00"  # not known yet: the recording's
    assert await twitch_vod_duration(make_ctx("live", "100", {"type": "live", "duration": 7000.0}), vod) == 7000.0


def test_file_chapters_follow_a_live_recording(make_ctx):
    import datetime as dt

    from archive_common.models import Vod
    from archive_worker import planning
    from archive_worker.steps.metadata import file_chapters

    start = dt.datetime(2026, 10, 8, 12, tzinfo=dt.UTC)
    chapters = [planning.chapter("1", "Celeste", None, 0, 600, False)]
    vod = Vod(id="100", title="t", created_at=start, duration="00:10:00", chapters=chapters)
    timeline = [[0.0, start.timestamp() + 60, 540.0]]  # the recording started a minute in
    assert file_chapters(make_ctx("archive", "100", {"timeline": timeline}), vod) == chapters
    assert file_chapters(make_ctx("live", "100", {"type": "live"}), vod) == chapters  # recorded before timelines
    moved = file_chapters(make_ctx("live", "100", {"type": "live", "timeline": timeline}), vod)
    assert moved is not None and [(c["start"], c["end"]) for c in moved] == [(0, 540)]
