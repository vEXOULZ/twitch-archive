"""YouTube steps: upload parts, then rewrite descriptions (part links + chapters)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archive_common.db import get_sessionmaker
from archive_common.models import Job, Vod
from archive_common.timeutil import hhmmss_to_seconds
from sqlalchemy import select

from .. import ffmpeg, planning
from ..context import JobContext
from ..vods import drop_youtube_entries, splice_reason
from .metadata import file_chapters, part_gaps, vod_duration


def _publishing_disabled(ctx: JobContext) -> str | None:
    s = ctx.settings
    if s.dry_run:
        return "dry run"
    if not s.youtube_upload:
        return "youtube_upload is off"
    if ctx.video_type == "vod" and s.live_record and not s.multi_track:
        return "live_record is on without multi_track: only the live copy is uploaded"
    return None


async def _save_entry(vod_id: str, entry: dict[str, Any]) -> None:
    """Upsert one {id,type,duration,part,thumbnail_url[,gaps]} into vods.youtube (row-locked,
    since the live and VOD jobs of one stream may finish at the same time). A re-upload of a part
    (a job without the recording's timeline) keeps the gaps of the upload it replaces."""
    async with get_sessionmaker()() as s:
        vod = (await s.execute(select(Vod).where(Vod.id == vod_id).with_for_update())).scalar_one()
        replaced: dict[str, Any] = next(
            (e for e in vod.youtube or [] if e.get("type") == entry["type"] and e.get("part") == entry["part"]), {}
        )
        if "gaps" not in entry and replaced.get("gaps"):
            entry = {**entry, "gaps": replaced["gaps"]}
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
    status = planning.privacy(ctx.video_type, s.youtube_public, s.live_record)
    uploaded: dict[str, dict[str, Any]] = ctx.payload.setdefault("uploaded", {})
    all_parts = planning.plan_parts(
        await vod_duration(ctx, vod), file_chapters(ctx, vod), s.restricted_games, s.split_duration
    )
    gaps = part_gaps(ctx, vod, all_parts)

    # A re-archive (``replace``) deletes each upload it replaces from YouTube once the new one is saved.
    replacing: dict[str, str] = {}
    if ctx.payload.get("replace"):
        replacing = ctx.payload.setdefault(
            "replacing",
            {str(e.get("part") or 1): e["id"] for e in vod.youtube or [] if e.get("type") == ctx.video_type},
        )
        await ctx.save()

    parts = ctx.payload["parts"]
    for part in parts:
        key = str(part["number"])
        if key in uploaded:
            continue
        path = Path(part["path"])
        title = planning.video_title(s.channel, ctx.video_type, vod.created_at, s.timezone, part["number"], total)
        ctx.log.info("uploading %s as %r (%s)", path.name, title, status)

        def progress(pct: int, number=part["number"]) -> None:  # type: ignore[no-untyped-def]
            ctx.progress(pct, 100, "percent", f"uploading part {number}: {pct}%")

        res = await ctx.deps.youtube.upload(
            path, title=title, description=description, privacy_status=status, on_progress=progress
        )
        thumbs = (res.get("snippet") or {}).get("thumbnails") or {}
        entry = {
            "id": res["id"],
            "type": ctx.video_type,
            "duration": planning.num_seconds(await ffmpeg.probe_duration(path)),
            "part": part["number"],
            "thumbnail_url": (thumbs.get("medium") or {}).get("url") or planning.youtube_thumbnail(res["id"]),
        }
        if gaps.get(part["number"]):
            entry["gaps"] = gaps[part["number"]]  # VOD time this part's recording missed (ad breaks)
        await _save_entry(vod.id, entry)
        uploaded[key] = entry
        await ctx.save()
        ctx.log.info("uploaded part %s -> https://youtu.be/%s", key, res["id"])
        done = sum(1 for p in parts if str(p["number"]) in uploaded)
        ctx.progress(done, len(parts), "parts", f"uploaded {done}/{len(parts)} parts")

    for key, entry in uploaded.items():
        old = replacing.get(key)
        if old and old != entry["id"]:
            await _delete_replaced(ctx, old)
    # The whole VOD again in fewer parts (a longer split_duration, say): the old parts past the new last one go.
    whole = ctx.payload.get("start_part") is None and ctx.payload.get("end_part") is None
    if replacing and whole and len(uploaded) == total:
        extra = {old for key, old in replacing.items() if int(key) > total}
        for old in sorted(extra):
            await _delete_replaced(ctx, old)
        gone = extra & set(ctx.payload.get("removed") or [])  # one YouTube refused stays listed, to delete later
        if gone:
            await drop_youtube_entries(vod.id, gone)


async def _delete_replaced(ctx: JobContext, video_id: str) -> None:
    """Delete an upload a re-archive replaced. A failure is logged and kept in the payload (``not_removed``)
    rather than failing the job: the new upload is in place, and the old one is left for YouTube Studio (or, for a
    part past the new last one, still listed, for the dashboard)."""
    removed: list[str] = ctx.payload.setdefault("removed", [])
    if video_id in removed:
        return
    try:
        found = await ctx.deps.youtube.delete(video_id)
    except Exception as exc:
        ctx.log.warning("could not delete replaced upload %s from YouTube: %s", video_id, exc)
        ctx.payload.setdefault("not_removed", []).append(video_id)
        await ctx.save()
        return
    ctx.log.info("deleted replaced upload %s%s", video_id, "" if found else " (already gone from YouTube)")
    removed.append(video_id)
    await ctx.save()


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
    chapters = file_chapters(ctx, vod)
    windows = {p.number: p for p in planning.plan_parts(duration, chapters, s.restricted_games, s.split_duration)}
    base = planning.base_description(s.domain_name, vod.id, vod.title, s.youtube_description)
    for entry in entries:
        number = int(entry.get("part") or 1)
        window = windows.get(number)
        lines = planning.chapter_lines(chapters, window, s.restricted_games) if window else []
        text = planning.full_description(base, number, entries, lines)
        await ctx.deps.youtube.update_description(entry["id"], text)
        ctx.log.info("described %s part %d", entry["id"], number)


# ── Gaps backfill ─────────────────────────────────────────────────────────


def recorded_gaps(
    payload: dict[str, Any], vod_seconds: float, vod_start: float, chapters: Any, restricted_games: list[str]
) -> dict[str, list[list[float]]]:
    """YouTube id -> ``gaps`` for the uploads of a finished ``live`` job, from its payload (the
    recording's ``timeline``, its ``parts`` and what it ``uploaded``). Empty when the payload lacks
    any of them, or holds only some of the recording's parts."""
    parts = [planning.Part(int(p["number"]), float(p["start"]), float(p["end"])) for p in payload.get("parts") or []]
    if not parts or len(parts) != int(payload.get("total_parts") or len(parts)):
        return {}
    timeline = payload.get("timeline") or []
    gaps = planning.part_gaps(vod_seconds, vod_start, chapters, restricted_games, timeline, parts)
    uploaded = payload.get("uploaded") or {}
    return {uploaded[str(n)]["id"]: g for n, g in gaps.items() if str(n) in uploaded}


async def live_gaps_backfill(ctx: JobContext) -> None:
    """``gaps`` on the live uploads made before they were saved, from each finished ``live``
    job's payload. A VOD whose live uploads already have gaps is left alone, as is a merged,
    split or synthetic one (its length and chapters are no longer the recording's VOD's).
    ``payload.vod_ids`` limits it to those VODs. Running it again changes nothing."""
    stmt = select(Job.vod_id, Job.payload).where(Job.kind == "live", Job.state == "done", Job.vod_id.is_not(None))
    if ctx.payload.get("vod_ids"):
        stmt = stmt.where(Job.vod_id.in_([str(v) for v in ctx.payload["vod_ids"]]))
    async with get_sessionmaker()() as s:
        found = (await s.execute(stmt.order_by(Job.id))).all()
    updated = 0
    for vod_id, payload in found:
        if not payload.get("timeline") or await splice_reason(vod_id):
            continue
        async with get_sessionmaker()() as s:
            vod = (await s.execute(select(Vod).where(Vod.id == vod_id).with_for_update())).scalar_one_or_none()
            live = [e for e in (vod.youtube if vod else None) or [] if e.get("type") == "live"]
            if vod is None or not live or any("gaps" in e for e in live):
                continue
            gaps = recorded_gaps(
                payload,
                hhmmss_to_seconds(vod.duration),
                vod.created_at.timestamp(),
                vod.chapters,
                ctx.settings.restricted_games,
            )
            if not any(e.get("id") in gaps for e in live):
                continue
            vod.youtube = [
                {**e, "gaps": gaps[e["id"]]} if e.get("type") == "live" and e.get("id") in gaps else e
                for e in vod.youtube
            ]
            await s.commit()
            updated += 1
            ctx.log.info("vod %s: gaps on %d live upload(s)", vod_id, sum(e.get("id") in gaps for e in live))
    ctx.log.info("saved gaps on %d VOD(s) of %d finished live job(s)", updated, len(found))
