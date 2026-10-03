"""Seek-bar previews (``archive_common.previews``): sprite sheets per YouTube upload, for the sites' timeline.

``previews`` runs after ``upload`` in every job that uploads, on the part files it just uploaded. Uploads made
before that get theirs from ``previews_fetch``: it downloads the upload's smallest video-only stream from YouTube
with yt-dlp, makes the sheets and deletes the download. ``previews_backfill`` queues one per VOD that has uploads
without previews. Previews are an extra: a part that fails is logged and skipped, never failing an archive job.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

from archive_common import previews as pv
from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from sqlalchemy import select

from .. import ffmpeg
from ..context import JobContext, StepError


async def _set_preview(vod_id: str, youtube_id: str, preview: dict[str, Any]) -> bool:
    """Put ``preview`` on the VOD's upload ``youtube_id`` (row-locked, like publish's entry upsert); False when
    the upload is no longer listed (replaced or removed meanwhile)."""
    async with get_sessionmaker()() as s:
        vod = (await s.execute(select(Vod).where(Vod.id == vod_id).with_for_update())).scalar_one_or_none()
        if vod is None:
            return False
        entries = [dict(e) for e in vod.youtube or []]
        found = False
        for e in entries:
            if e.get("id") == youtube_id:
                e["preview"] = preview
                found = True
        if found:
            vod.youtube = entries
            await s.commit()
        return found


async def _make(ctx: JobContext, src: Path, youtube_id: str) -> bool:
    """Sheets of ``src`` for the upload ``youtube_id``, saved on its entry; False (logged) on failure."""
    try:
        out = pv.directory(ctx.settings.previews_dir, youtube_id)
        frames = await ffmpeg.preview_sheets(src, out)
    except (ffmpeg.FfmpegError, OSError, ValueError) as exc:
        ctx.log.warning("previews of %s failed: %s", youtube_id, exc)
        return False
    if not await _set_preview(ctx.require_vod_id(), youtube_id, pv.info(frames)):
        ctx.log.warning("upload %s is no longer on VOD %s; its previews are left unused", youtube_id, ctx.vod_id)
        return False
    ctx.log.info("previews of %s: %d frames", youtube_id, frames)
    return True


async def previews(ctx: JobContext) -> None:
    """Sheets for each part this job uploaded (``payload.uploaded``). A dry run uploads nothing: its parts' sheets
    go to ``<work dir>/previews/<part>`` to look at instead."""
    s = ctx.settings
    if not s.previews:
        ctx.log.info("skipping previews: previews are off")
        return
    parts = ctx.payload.get("parts") or []
    if s.dry_run:
        for part in parts:
            out = ctx.work_dir / "previews" / str(part["number"])
            try:
                frames = await ffmpeg.preview_sheets(Path(part["path"]), out)
            except (ffmpeg.FfmpegError, OSError) as exc:
                ctx.log.warning("previews of part %s failed: %s", part["number"], exc)
                continue
            ctx.log.info("dry run: %d preview frames of part %s in %s", frames, part["number"], out)
        return
    uploaded: dict[str, dict[str, Any]] = ctx.payload.get("uploaded") or {}
    done = {e.get("id") for e in (await ctx.get_vod()).youtube or [] if e.get("preview")}
    todo = [(p, uploaded[str(p["number"])]["id"]) for p in parts if str(p["number"]) in uploaded]
    todo = [(p, yid) for p, yid in todo if yid not in done]
    for i, (part, youtube_id) in enumerate(todo):
        ctx.progress(i, len(todo), "parts", f"previews of part {part['number']} ({i + 1}/{len(todo)})")
        await _make(ctx, Path(part["path"]), youtube_id)
    ctx.progress(len(todo), len(todo), "parts", "previews done")


async def _download(ctx: JobContext, youtube_id: str, into: Path) -> Path:
    """The upload's smallest useful video-only stream (240p or less), downloaded into ``into``."""
    into.mkdir(parents=True, exist_ok=True)
    args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--quiet",
        "--no-warnings",
        "--no-progress",
        "--no-playlist",
        "-f",
        "bv[height<=240]/wv/w",
        "-o",
        str(into / f"{youtube_id}.%(ext)s"),
        "--print",
        "after_move:filepath",
        *ctx.settings.previews_ytdlp_args,
        "--",
        f"https://www.youtube.com/watch?v={youtube_id}",
    ]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise StepError(f"yt-dlp failed for {youtube_id}: {err.decode('utf-8', 'replace')[-1000:].strip()}")
    lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
    path = Path(lines[-1].strip()) if lines else None
    if path is None or not path.exists():
        raise StepError(f"yt-dlp reported no file for {youtube_id}")
    return path


async def previews_fetch(ctx: JobContext) -> None:
    """Sheets for the VOD's uploads that have none (``payload.youtube_ids``: only those), each from a download
    of the upload that is deleted right after. Downloads are ``previews_fetch_pause_seconds`` apart, and these
    jobs share one lock (``jobs._lock``), so a backfill reaches YouTube slowly. Fails only when every upload
    failed (so the runtime retries later); otherwise the failures are logged."""
    vod = await ctx.get_vod()
    only = {str(v) for v in ctx.payload.get("youtube_ids") or []}
    todo = [
        e["id"]
        for e in vod.youtube or []
        if isinstance(e.get("id"), str)
        and pv.YOUTUBE_ID.match(e["id"])
        and not e.get("preview")
        and (not only or e["id"] in only)
    ]
    if not todo:
        ctx.log.info("every upload of %s has previews", vod.id)
        return
    work = ctx.work_dir / "previews-fetch"
    failed: list[str] = []
    try:
        for i, youtube_id in enumerate(todo):
            ctx.progress(i, len(todo), "parts", f"previews of {youtube_id} ({i + 1}/{len(todo)})")
            if i:
                await asyncio.sleep(ctx.settings.previews_fetch_pause_seconds)
            try:
                path = await _download(ctx, youtube_id, work)
            except StepError as exc:
                ctx.log.warning("%s", exc)
                failed.append(youtube_id)
                continue
            try:
                if not await _make(ctx, path, youtube_id):
                    failed.append(youtube_id)
            finally:
                path.unlink(missing_ok=True)
    finally:
        await asyncio.to_thread(shutil.rmtree, work, True)
        try:
            ctx.work_dir.rmdir()  # only if empty
        except OSError:
            pass
    ctx.progress(len(todo), len(todo), "parts", "previews done")
    if failed and len(failed) == len(todo):
        raise StepError(f"no previews made for {', '.join(failed)}")
    if failed:
        ctx.log.warning("no previews for %s", ", ".join(failed))


async def previews_backfill(ctx: JobContext) -> None:
    """Queue a ``previews_fetch`` per VOD with uploads that have no previews (``payload.vod_ids``: only those),
    newest first. They are its children and run one at a time; VODs with one queued already are skipped."""
    from .. import jobs  # jobs imports the steps

    stmt = select(Vod).order_by(Vod.created_at.desc())
    if ctx.payload.get("vod_ids"):
        stmt = stmt.where(Vod.id.in_([str(v) for v in ctx.payload["vod_ids"]]))
    async with get_sessionmaker()() as s:
        vods = [
            v
            for v in (await s.execute(stmt)).scalars()
            if any(isinstance(e, dict) and e.get("id") and not e.get("preview") for e in v.youtube or [])
        ]
    queued = 0
    for i, vod in enumerate(vods):
        ctx.progress(100 * i / len(vods), 100, "percent", f"previews backfill: {vod.id} ({i + 1}/{len(vods)})")
        if await jobs.find_active("previews_fetch", vod_id=vod.id):
            ctx.log.info("skipping %s: a previews_fetch job is already queued or running", vod.id)
            continue
        job_id = await ctx.enqueue("previews_fetch", vod.id, {"backfill": True})
        ctx.log.info("queued previews for %s: job %d", vod.id, job_id)
        queued += 1
    ctx.progress(100, 100, "percent", "previews backfill queued")
    ctx.log.info("previews backfill: %d job(s) queued for %d VOD(s)", queued, len(vods))
