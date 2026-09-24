"""File steps: HLS -> MP4, source resolution, splitting, DMCA edits, cleanup."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from archive_common.timeutil import format_hhmmss

from .. import ffmpeg, planning
from ..context import JobContext, StepError
from ..vods import upsert_vod, vod_id_for_stream
from .capture import capture
from .metadata import vod_duration


async def resolve_vod(ctx: JobContext) -> None:
    """Live jobs start before Twitch has a VOD; attach the vod id by stream id."""
    if ctx.vod_id:
        return
    stream_id = str(ctx.payload["stream_id"])
    for attempt in range(30):
        vod_id = await vod_id_for_stream(stream_id)
        if vod_id is None and ctx.deps.helix.configured:
            video = await ctx.deps.helix.video_for_stream(ctx.settings.twitch_id, stream_id)
            if video:
                await upsert_vod(video)
                vod_id = video["id"]
        if vod_id:
            ctx.vod_id = vod_id
            await ctx.save()
            ctx.log.info("stream %s is vod %s", stream_id, vod_id)
            return
        await asyncio.sleep(60)
    raise StepError(f"no Twitch VOD found for stream {stream_id} (VODs disabled on the channel?)")


async def finalize(ctx: JobContext) -> None:
    out = ctx.default_mp4
    if not (out.exists() and ctx.payload.get("duration")):
        playlist = ctx.hls_dir / "index.m3u8"
        if not playlist.exists():
            raise StepError(f"{playlist} missing; nothing to convert")
        ctx.log.info("converting %s -> %s", playlist, out)
        await ffmpeg.hls_to_mp4(playlist, out, fmp4=bool(ctx.payload.get("fmp4")))
        ctx.payload["duration"] = await ffmpeg.probe_duration(out)
        await ctx.save()
    ctx.payload.pop("mp4", None)
    if ctx.video_type == "vod":
        await ctx.update_vod(duration=format_hhmmss(ctx.payload["duration"]))
    ctx.log.info("mp4 ready: %s (%s)", out, format_hhmmss(ctx.payload["duration"]))


async def ensure_source(ctx: JobContext) -> None:
    """Make sure a full-length MP4 exists: a given path, a previous download,
    or (for Twitch VOD copies) a fresh one-shot HLS download."""
    given = ctx.payload.get("path")
    if given:
        path = Path(given)
        if not path.exists():
            raise StepError(f"{path} does not exist")
        ctx.payload["mp4"] = str(path)
    elif not ctx.default_mp4.exists():
        if ctx.video_type == "live":
            raise StepError(f"live recording {ctx.default_mp4} not found")
        ctx.log.info("no local copy of %s; downloading", ctx.vod_id)
        await capture(ctx, one_shot=True)
        await finalize(ctx)
        return
    if not ctx.payload.get("duration"):
        ctx.payload["duration"] = await ffmpeg.probe_duration(ctx.source_mp4)
    await ctx.save()


async def split(ctx: JobContext) -> None:
    s = ctx.settings
    vod = await ctx.get_vod()
    duration = await vod_duration(ctx, vod)
    all_parts = planning.plan_parts(duration, vod.chapters, s.restricted_games, s.split_duration)
    if not all_parts:
        raise StepError("nothing to upload (VOD is empty or entirely restricted)")
    parts = planning.select_parts(all_parts, ctx.payload.get("start_part"), ctx.payload.get("end_part"))
    if not parts:
        raise StepError(f"no parts in range (VOD has {len(all_parts)})")
    done = {p["number"]: p for p in ctx.payload.get("parts", []) if Path(p["path"]).exists()}
    src = ctx.source_mp4
    out: list[dict] = []
    for part in parts:
        if part.number in done:
            out.append(done[part.number])
            continue
        if len(all_parts) == 1 and part.start == 0 and part.end >= duration - 1:
            path = src  # whole video, no cut needed
        else:
            path = ctx.parts_dir / f"{src.stem}-part{part.number}.mp4"
            ctx.log.info("cutting part %d: %s + %s", part.number, format_hhmmss(part.start),
                         format_hhmmss(part.duration))
            await ffmpeg.cut(src, path, part.start, part.duration)
        out.append({"number": part.number, "start": part.start, "end": part.end, "path": str(path)})
        ctx.payload["parts"] = out
        await ctx.save()
    ctx.payload["parts"] = out
    ctx.payload["total_parts"] = len(all_parts)
    await ctx.save()


async def dmca_edit(ctx: JobContext) -> None:
    """Mute / black out claimed ranges. ``dmca`` jobs edit the full source
    (claims are relative to it); ``part_dmca`` jobs edit the single cut part."""
    if ctx.payload.get("dmca_done"):
        return
    plan = planning.plan_dmca(ctx.payload.get("claims") or [])
    if plan.empty:
        raise StepError("no blocking claims to mute or black out")
    targets = [Path(p["path"]) for p in ctx.payload.get("parts", [])] if ctx.kind == "part_dmca" else [ctx.source_mp4]
    work = ctx.work_dir / "dmca"
    edited: list[Path] = []
    for src in targets:
        cur = src
        for i, (a, b) in enumerate(plan.blackout):
            nxt = work / f"{src.stem}-black{i}.mp4"
            ctx.log.info("blackout %s %.0f-%.0f", src.name, a, b)
            await ffmpeg.blackout(cur, nxt, a, b, work)
            if cur != src:
                cur.unlink(missing_ok=True)
            cur = nxt
        if plan.mute:
            nxt = work / f"{src.stem}-muted.mp4"
            ctx.log.info("muting %d range(s) in %s", len(plan.mute), src.name)
            await ffmpeg.mute(cur, nxt, plan.mute)
            if cur != src:
                cur.unlink(missing_ok=True)
            cur = nxt
        edited.append(cur)
    if ctx.kind == "part_dmca":
        for p, path in zip(ctx.payload["parts"], edited):
            p["path"] = str(path)
    else:
        ctx.payload["mp4"] = str(edited[0])
        ctx.payload.pop("parts", None)
    ctx.payload["dmca_done"] = True
    await ctx.save()


async def cleanup(ctx: JobContext) -> None:
    s = ctx.settings
    if s.dry_run:
        ctx.log.info("dry run: keeping files in %s", ctx.work_dir)
        return
    removed: list[Path] = []
    for d in (ctx.parts_dir, ctx.work_dir / "dmca"):
        if d.exists():
            await asyncio.to_thread(shutil.rmtree, d, True)
            removed.append(d)
    if not s.keep_hls and ctx.hls_dir.exists():
        await asyncio.to_thread(shutil.rmtree, ctx.hls_dir, True)
        removed.append(ctx.hls_dir)
    if not s.keep_mp4 and ctx.default_mp4.exists():
        ctx.default_mp4.unlink()
        removed.append(ctx.default_mp4)
    try:
        ctx.work_dir.rmdir()  # only if empty
    except OSError:
        pass
    ctx.log.info("cleanup removed: %s", ", ".join(p.name for p in removed) or "nothing")
