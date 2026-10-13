"""ffmpeg / ffprobe subprocess wrappers.

Every command writes to ``<out>.part`` and renames on success, so a killed
worker never leaves a truncated file that looks finished.

The heavy operations (remux, cut, mute, blackout, preview sheets) run one of a
type at a time across the worker's jobs (``_one_at_a_time``); a job that wants one
while another job has it waits its turn. ``probe_duration`` is cheap and never waits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import weakref
from collections.abc import Callable, Coroutine
from functools import wraps
from pathlib import Path
from typing import Any

from archive_common import previews as pv

log = logging.getLogger(__name__)

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")


class FfmpegError(RuntimeError):
    pass


# One lock per (event loop, operation): an asyncio.Lock belongs to the loop it first waits on.
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()


type _Op[**P, T] = Callable[P, Coroutine[Any, Any, T]]


def _one_at_a_time[**P, T](op: str) -> Callable[[_Op[P, T]], _Op[P, T]]:
    """Run the decorated operation under the ``op`` lock: one ``op`` at a time in this worker, while
    different ops still overlap. Waiting can be cancelled like the run itself."""

    def wrap(fn: _Op[P, T]) -> _Op[P, T]:
        @wraps(fn)
        async def run(*args: P.args, **kwargs: P.kwargs) -> T:
            lock = _locks.setdefault(asyncio.get_running_loop(), {}).setdefault(op, asyncio.Lock())
            if lock.locked():
                log.info("ffmpeg %s: waiting for the one already running", op)
            async with lock:
                return await fn(*args, **kwargs)

        return run

    return wrap


async def _run(args: list[str]) -> str:
    log.debug("exec: %s", " ".join(args))
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace")[-2000:]
        raise FfmpegError(f"{Path(args[0]).name} exited with {proc.returncode}: {tail}")
    return out.decode("utf-8", "replace")


async def _ffmpeg_to(out: Path, args: list[str]) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.unlink(missing_ok=True)
    await _run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args, "-f", "mp4", str(tmp)])
    os.replace(tmp, out)
    return out


async def probe_duration(path: Path) -> float:
    out = await _run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    return float(json.loads(out)["format"]["duration"])


@_one_at_a_time("remux")
async def hls_to_mp4(playlist: Path, out: Path, *, fmp4: bool = False) -> Path:
    extra = ["-avoid_negative_ts", "make_zero", "-fflags", "+genpts"] if fmp4 else []
    return await _ffmpeg_to(
        out,
        [
            "-thread_queue_size",
            "1024",
            "-allowed_extensions",
            "ALL",
            "-i",
            str(playlist),
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-movflags",
            "+faststart",
            *extra,
        ],
    )


# Input seeking (``-ss`` before ``-i``) jumps straight to a keyframe; output seeking
# (``-ss`` after ``-i``) then trims exactly, but reads every packet before its target.
# Seeking in two stages gets both: the jump lands SEEK_MARGIN seconds early, well
# over Twitch's 2 s keyframe interval, and the exact trim only reads that margin.
# With stream copy the result is packet-for-packet identical to output seeking alone.
SEEK_MARGIN = 30.0


def _seek_args(src: Path, start: float) -> list[str]:
    """Input arguments that start reading ``src`` at ``start`` seconds."""
    if start <= 0:
        return ["-i", str(src)]
    margin = min(SEEK_MARGIN, start)
    return ["-ss", f"{start - margin:.3f}", "-i", str(src), "-ss", f"{margin:.3f}"]


@_one_at_a_time("cut")
async def cut(src: Path, out: Path, start: float, duration: float | None = None, *, faststart: bool = True) -> Path:
    """Stream-copy ``duration`` seconds (default: to the end) starting at ``start``.

    ``faststart=False`` skips the second pass that moves the index to the front,
    for intermediate files that are only joined again.
    """
    length = ["-t", f"{duration:.3f}"] if duration is not None else []  # a length, not an end
    return await _ffmpeg_to(
        out,
        [
            *_seek_args(src, start),
            *length,
            "-map",
            "0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            *(["-movflags", "+faststart"] if faststart else []),
        ],
    )


@_one_at_a_time("previews")
async def preview_sheets(src: Path, out_dir: Path) -> int:
    """Seek-bar sprite sheets of ``src`` into ``out_dir`` (``archive_common.previews`` has the layout); the
    number of frames. Only keyframes are decoded (Twitch has one every 2 s), so a 1080p part takes a minute or
    two. ``round=up`` takes, for each tile, the last frame at or before its time (``near`` would take one up to
    half an interval late). The sheets go to ``<out_dir>.part`` first and replace ``out_dir`` whole."""
    frames = pv.frame_count(await probe_duration(src))
    tmp = out_dir.with_name(out_dir.name + ".part")
    await asyncio.to_thread(shutil.rmtree, tmp, True)
    tmp.mkdir(parents=True)
    w, h = pv.WIDTH, pv.HEIGHT
    vf = (
        f"fps=1/{pv.INTERVAL}:round=up,scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,tile={pv.COLS}x{pv.ROWS}"
    )
    try:
        await _run(
            [
                FFMPEG,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-skip_frame",
                "nokey",
                "-i",
                str(src),
                "-map",
                "0:v:0",
                "-vf",
                vf,
                "-frames:v",
                str(pv.sheet_count(frames)),
                "-q:v",
                "5",
                "-start_number",
                "0",
                str(tmp / "%d.jpg"),
            ]
        )
        await asyncio.to_thread(shutil.rmtree, out_dir, True)
        os.replace(tmp, out_dir)
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp, True)
    return frames


@_one_at_a_time("mute")
async def mute(src: Path, out: Path, ranges: list[tuple[float, float]]) -> Path:
    """Silence audio in [start, end) ranges; video is stream-copied."""
    filters = ",".join(f"volume=0:enable='between(t,{a:g},{b:g})'" for a, b in ranges)
    return await _ffmpeg_to(
        out,
        [
            "-i",
            str(src),
            "-map",
            "0",
            "-c:v",
            "copy",
            "-af",
            filters,
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
        ],
    )


@_one_at_a_time("blackout")
async def blackout(src: Path, out: Path, ranges: list[tuple[float, float]], work: Path) -> Path:
    """Replace video in each [start, end) range with black, keeping audio, in one pass.

    Only the claimed clips are re-encoded; the stretches around them are
    stream-copied, and all pieces are joined once with the concat demuxer (as the
    legacy code did per range). ``ranges`` must be sorted and non-overlapping, as
    ``planning.plan_dmca`` returns them.
    """
    work.mkdir(parents=True, exist_ok=True)
    total = await probe_duration(src)
    pieces: list[Path] = []
    pos = 0.0
    try:
        for i, (start, end) in enumerate(ranges):
            if start > pos:
                pieces.append(await cut(src, work / f"bo-{i}-copy.mp4", pos, start - pos, faststart=False))
            pieces.append(
                await _ffmpeg_to(
                    work / f"bo-{i}-black.mp4",
                    [
                        "-ss",
                        f"{start:.3f}",
                        "-i",
                        str(src),
                        "-t",
                        f"{end - start:.3f}",
                        "-map",
                        "0",
                        "-vf",
                        "geq=0:128:128",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "30",
                        "-c:a",
                        "copy",
                    ],
                )
            )
            pos = end
        if pos < total - 0.05:
            pieces.append(await cut(src, work / "bo-tail.mp4", pos, faststart=False))
        listing = work / "bo-list.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")
        pieces.append(listing)
        return await _ffmpeg_to(
            out, ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart"]
        )
    finally:
        for p in pieces:
            p.unlink(missing_ok=True)
