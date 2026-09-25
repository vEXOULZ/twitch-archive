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


async def cut(src: Path, out: Path, start: float, duration: float | None = None, *,
              faststart: bool = True) -> Path:
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
            "-map", "0",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            *(["-movflags", "+faststart"] if faststart else []),
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
                        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}", "-map", "0",
                        "-vf", "geq=0:128:128", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                        "-c:a", "copy",
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
