import asyncio
import os

from archive_worker import ffmpeg


async def test_one_operation_of_a_type_at_a_time(monkeypatch, tmp_path):
    """Jobs asking for the same operation take turns; different operations overlap."""
    running: list[str] = []
    peak: dict[str, int] = {}

    async def fake_run(args):
        op = args[-1].rsplit("/", 1)[-1].split("-")[0]  # ".../<op>-<n>.mp4.part"
        running.append(op)
        peak[op] = max(peak.get(op, 0), running.count(op))
        await asyncio.sleep(0.05)
        running.remove(op)
        return ""

    monkeypatch.setattr(ffmpeg, "_run", fake_run)
    monkeypatch.setattr(os, "replace", lambda *_: None)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.gather(
        *(ffmpeg.mute(tmp_path / "src.mp4", tmp_path / f"mute-{i}.mp4", [(0, 1)]) for i in range(3)),
        *(ffmpeg.hls_to_mp4(tmp_path / "index.m3u8", tmp_path / f"remux-{i}.mp4") for i in range(3)),
    )
    assert peak == {"mute": 1, "remux": 1}
    assert loop.time() - started < 0.25  # 3 × 50 ms each, the two kinds side by side


async def test_waiting_can_be_cancelled(monkeypatch, tmp_path):
    gate = asyncio.Event()

    async def fake_run(args):
        await gate.wait()
        return ""

    monkeypatch.setattr(ffmpeg, "_run", fake_run)
    monkeypatch.setattr(os, "replace", lambda *_: None)
    first = asyncio.create_task(ffmpeg.mute(tmp_path / "a.mp4", tmp_path / "a-out.mp4", [(0, 1)]))
    second = asyncio.create_task(ffmpeg.mute(tmp_path / "b.mp4", tmp_path / "b-out.mp4", [(0, 1)]))
    await asyncio.sleep(0.01)
    second.cancel()  # cancelled while waiting for the first
    gate.set()
    assert await first == tmp_path / "a-out.mp4"
    assert second.cancelled()
    third = ffmpeg.mute(tmp_path / "c.mp4", tmp_path / "c-out.mp4", [(0, 1)])
    assert await asyncio.wait_for(third, 1) == tmp_path / "c-out.mp4"  # the lock was released
