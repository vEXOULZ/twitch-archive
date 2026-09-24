"""ffmpeg / ffprobe subprocess wrappers.

Every command writes to ``<out>.part`` and renames on success, so a killed
worker never leaves a truncated file that looks finished.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")


class FfmpegError(RuntimeError):
    pass


async def _run(args: list[str]) -> str:
    log.debug("exec: %s", " ".join(args))
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
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
    out = await _run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]
    )
    return float(json.loads(out)["format"]["duration"])


async def hls_to_mp4(playlist: Path, out: Path, *, fmp4: bool = False) -> Path:
    extra = ["-avoid_negative_ts", "make_zero", "-fflags", "+genpts"] if fmp4 else []
    return await _ffmpeg_to(
        out,
        [
            "-thread_queue_size", "1024",
            "-allowed_extensions", "ALL",
            "-i", str(playlist),
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            *extra,
        ],
    )


async def cut(src: Path, out: Path, start: float, duration: float) -> Path:
    """Stream-copy ``duration`` seconds starting at ``start``.

    ``-ss`` after ``-i`` (output seeking) avoids the seek artefacts upstream saw
    with input seeking; ``-t`` is a length, not an end timestamp.
    """
    return await _ffmpeg_to(
        out,
        [
            "-i", str(src),
            "-ss", f"{start:.3f}",
            "-t", f"{duration:.3f}",
            "-map", "0",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
        ],
    )


async def mute(src: Path, out: Path, ranges: list[tuple[float, float]]) -> Path:
    """Silence audio in [start, end) ranges; video is stream-copied."""
    filters = ",".join(f"volume=0:enable='between(t,{a:g},{b:g})'" for a, b in ranges)
    return await _ffmpeg_to(
        out,
        ["-i", str(src), "-map", "0", "-c:v", "copy", "-af", filters, "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart"],
    )


async def blackout(src: Path, out: Path, start: float, end: float, work: Path) -> Path:
    """Replace video in [start, end) with black, keeping audio.

    Only the claimed clip is re-encoded; head and tail are stream-copied and the
    three pieces are joined with the concat demuxer (as the legacy code did).
    """
    work.mkdir(parents=True, exist_ok=True)
    head, clip, tail = work / "bo-head.mp4", work / "bo-clip.mp4", work / "bo-tail.mp4"
    pieces: list[Path] = []
    if start > 0:
        await _ffmpeg_to(head, ["-i", str(src), "-t", f"{start:.3f}", "-map", "0", "-c", "copy"])
        pieces.append(head)
    await _ffmpeg_to(
        clip,
        [
            "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}", "-map", "0",
            "-vf", "geq=0:128:128", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-c:a", "copy",
        ],
    )
    pieces.append(clip)
    total = await probe_duration(src)
    if end < total - 0.05:
        await _ffmpeg_to(tail, ["-i", str(src), "-ss", f"{end:.3f}", "-map", "0", "-c", "copy",
                                "-avoid_negative_ts", "make_zero"])
        pieces.append(tail)
    listing = work / "bo-list.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")
    try:
        return await _ffmpeg_to(
            out, ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart"]
        )
    finally:
        for p in (*pieces, listing):
            p.unlink(missing_ok=True)
