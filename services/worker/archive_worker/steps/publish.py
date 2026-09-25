"""YouTube steps: upload parts, then rewrite descriptions (part links + chapters)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from archive_common.db import get_sessionmaker
from archive_common.models import Vod

from .. import ffmpeg, planning
from ..context import JobContext
from .metadata import vod_duration


def _publishing_disabled(ctx: JobContext) -> str | None:
    s = ctx.settings
    if s.dry_run:
        return "dry run"
    if not s.youtube_upload:
        return "youtube_upload is off"
    if ctx.video_type == "vod" and s.live_record and not s.multi_track:
        return "live_record is on without multi_track: only the live copy is uploaded"
    return None


async def _save_entry(vod_id: str, entry: dict) -> None:
    """Upsert one {id,type,duration,part,thumbnail_url} into vods.youtube (row-locked,
    since the live and VOD jobs of one stream may finish at the same time)."""
    async with get_sessionmaker()() as s:
        vod = (await s.execute(select(Vod).where(Vod.id == vod_id).with_for_update())).scalar_one()
        vod.youtube = planning.upsert_youtube_entry(vod.youtube, entry)
        if entry.get("thumbnail_url"):
            vod.thumbnail_url = entry["thumbnail_url"]
        await s.commit()


async def upload(ctx: JobContext) -> None:
    reason = _publishing_disabled(ctx)
    if reason:
        ctx.log.info("skipping upload: %s", reason)
        return
    s = ctx.settings
    vod = await ctx.get_vod()
    total = int(ctx.payload.get("total_parts") or len(ctx.payload["parts"]))
    description = planning.base_description(s.domain_name, vod.id, vod.title, s.youtube_description)
    status = planning.privacy(ctx.video_type, s.youtube_public, s.multi_track)
    uploaded: dict[str, dict] = ctx.payload.setdefault("uploaded", {})

    parts = ctx.payload["parts"]
    for part in parts:
        key = str(part["number"])
        if key in uploaded:
            continue
        path = Path(part["path"])
        title = planning.video_title(s.channel, ctx.video_type, vod.created_at, s.timezone, part["number"], total)
        ctx.log.info("uploading %s as %r (%s)", path.name, title, status)

        def progress(pct: int, number=part["number"]) -> None:
            ctx.progress(pct, 100, "percent", f"uploading part {number}: {pct}%")

        res = await ctx.deps.youtube.upload(path, title=title, description=description, privacy_status=status,
                                            on_progress=progress)
        thumbs = (res.get("snippet") or {}).get("thumbnails") or {}
        entry = {
            "id": res["id"],
            "type": ctx.video_type,
            "duration": planning.num_seconds(await ffmpeg.probe_duration(path)),
            "part": part["number"],
            "thumbnail_url": (thumbs.get("medium") or {}).get("url")
            or f"https://i.ytimg.com/vi/{res['id']}/mqdefault.jpg",
        }
        await _save_entry(vod.id, entry)
        uploaded[key] = entry
        await ctx.save()
        ctx.log.info("uploaded part %s -> https://youtu.be/%s", key, res["id"])
        done = sum(1 for p in parts if str(p["number"]) in uploaded)
        ctx.progress(done, len(parts), "parts", f"uploaded {done}/{len(parts)} parts")


async def describe(ctx: JobContext) -> None:
    """One videos.update per part: sibling part links + that part's chapters."""
    reason = _publishing_disabled(ctx)
    if reason:
        ctx.log.info("skipping describe: %s", reason)
        return
    s = ctx.settings
    vod = await ctx.get_vod()
    entries = [e for e in vod.youtube or [] if e.get("type") == ctx.video_type]
    if not entries:
        ctx.log.info("no %s uploads to describe", ctx.video_type)
        return
    duration = await vod_duration(ctx, vod)
    windows = {p.number: p for p in planning.plan_parts(duration, vod.chapters, s.restricted_games, s.split_duration)}
    base = planning.base_description(s.domain_name, vod.id, vod.title, s.youtube_description)
    for entry in entries:
        number = int(entry.get("part") or 1)
        window = windows.get(number)
        lines = planning.chapter_lines(vod.chapters, window, s.restricted_games) if window else []
        text = planning.full_description(base, number, entries, lines)
        await ctx.deps.youtube.update_description(entry["id"], text)
        ctx.log.info("described %s part %d", entry["id"], number)
