"""Metadata steps: chapters, chat replay, third-party emotes, manual chat import."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx
from archive_common import emote_providers as providers
from archive_common import http
from archive_common.db import get_sessionmaker
from archive_common.models import Emote, Log, Vod
from archive_common.timeutil import hhmmss_to_seconds, parse_ts
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from .. import planning
from ..context import JobContext, StepError
from ..timeline import EMOTE_SETS as CHANNEL_SETS
from ..vods import notify_rows_moved

CHAT_BATCH = 2500
DEFAULT_COLOR = "#999999"


async def vod_duration(ctx: JobContext, vod: Vod | None = None) -> float:
    vod = vod or await ctx.get_vod()
    return float(ctx.payload.get("duration") or hhmmss_to_seconds(vod.duration))


# ── Chapters ──────────────────────────────────────────────────────────────


async def chapters(ctx: JobContext) -> None:
    vod_id = ctx.require_vod_id()
    gql = ctx.deps.gql
    restricted = ctx.settings.restricted_games
    await ctx.refuse_if_spliced()  # even with force: they are the merged/split chapters
    vod = await ctx.get_vod()
    if vod.chapters_locked and ctx.payload.get("force") is not True:
        ctx.log.info("chapters of %s were edited by hand (locked); keeping them", vod_id)
        return
    duration = await vod_duration(ctx, vod)
    edges = await gql.video_moments(vod_id)
    if edges is None:
        ctx.log.warning("no chapter data for %s (VOD deleted?); keeping existing chapters", vod_id)
        return
    if edges:
        result = planning.chapters_from_moments(edges, duration, restricted)
    else:
        video = await gql.video_game(vod_id)
        game = (video or {}).get("game")
        box_art = None
        if game and ctx.deps.helix.configured:
            data = await ctx.deps.helix.get_game(game["id"])
            box_art = (data or {}).get("box_art_url")
        result = [planning.single_chapter(game, box_art, duration, restricted)]
    await ctx.update_vod(chapters=result)
    ctx.log.info("saved %d chapter(s)", len(result))


# ── Chat replay ───────────────────────────────────────────────────────────


def comment_row(vod_id: str, node: dict[str, Any]) -> dict[str, Any]:
    commenter = node.get("commenter") or {}
    message = node.get("message") or {}
    return {
        "id": node["id"],
        "vod_id": vod_id,
        "display_name": commenter.get("displayName"),
        "content_offset_seconds": int(node.get("contentOffsetSeconds") or 0),
        "message": message.get("fragments") or [],
        "user_badges": message.get("userBadges") or [],
        "user_color": message.get("userColor") or DEFAULT_COLOR,
        "created_at": parse_ts(node.get("createdAt")) or dt.datetime.now(dt.UTC),
    }


async def insert_comments(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    async with get_sessionmaker()() as s:
        for i in range(0, len(rows), CHAT_BATCH):
            await s.execute(insert(Log).on_conflict_do_nothing(index_elements=["id"]), rows[i : i + CHAT_BATCH])
        await s.commit()


async def chat(ctx: JobContext) -> None:
    """Crawl the VOD's chat replay. Resumes from the last stored offset."""
    if not ctx.settings.chat_download:
        ctx.log.info("chat download disabled")
        return
    vod_id = ctx.require_vod_id()
    await ctx.refuse_if_spliced()  # a merged VOD's rows past the join belong to the other VOD
    gql = ctx.deps.gql
    async with get_sessionmaker()() as s:
        offset = (
            await s.execute(select(func.max(Log.content_offset_seconds)).where(Log.vod_id == vod_id))
        ).scalar() or 0

    video = await gql.comments(vod_id, offset=offset)
    rows: list[dict[str, Any]] = []
    total = 0
    while True:
        comments = (video or {}).get("comments")
        if not comments:
            if total == 0:
                ctx.log.info("no comments for %s at offset %s", vod_id, offset)
            break
        edges = comments.get("edges") or []
        rows.extend(comment_row(vod_id, e["node"]) for e in edges if e.get("node"))
        if len(rows) >= CHAT_BATCH:
            await insert_comments(rows)
            total += len(rows)
            rows = []
        cursor = edges[-1].get("cursor") if edges else None
        if not cursor or not (comments.get("pageInfo") or {}).get("hasNextPage"):
            break
        await asyncio.sleep(0.15)
        video = await gql.comments(vod_id, cursor=cursor)
    await insert_comments(rows)
    total += len(rows)
    ctx.log.info("chat: stored %d comment(s) for %s", total, vod_id)


async def logs_manual(ctx: JobContext) -> None:
    """Import a chat JSON file ({"comments": {"edges": [...]}}), e.g. from TwitchDownloader."""
    vod_id = ctx.require_vod_id()
    path = Path(ctx.payload["path"])
    if not path.exists():
        raise StepError(f"{path} does not exist")
    data = await asyncio.to_thread(lambda: json.loads(path.read_text(encoding="utf-8")))  # can be hundreds of MB
    edges = ((data.get("comments") or {}).get("edges")) or []
    rows = [comment_row(vod_id, e["node"]) for e in edges if e.get("node")]
    await insert_comments(rows)
    ctx.log.info("imported %d comment(s) from %s", len(rows), path)


# ── Emotes ────────────────────────────────────────────────────────────────


async def _json(url: str, ctx: JobContext):  # type: ignore[no-untyped-def]
    try:
        return (await http.request("GET", url)).json()
    except Exception as exc:
        ctx.log.warning("emote fetch %s failed: %s", url, exc)
        return None


async def _fetch(endpoints: dict[str, providers.Endpoint], ctx: JobContext) -> dict[str, list[dict[str, Any]]]:
    """Each provider's emotes from ``endpoints``; a provider that fails gets an empty list."""
    responses = await asyncio.gather(*(_json(url, ctx) for url, _ in endpoints.values()))
    return {p: parse(data) for (p, (_, parse)), data in zip(endpoints.items(), responses, strict=True)}


async def fetch_global_emotes(ctx: JobContext) -> dict[str, list[dict[str, Any]]]:
    """The providers' current global sets."""
    return await _fetch(providers.GLOBAL, ctx)


async def fetch_emotes(ctx: JobContext, twitch_id: str) -> dict[str, Any]:
    channel, global_emotes = await asyncio.gather(_fetch(providers.channel(twitch_id), ctx), fetch_global_emotes(ctx))
    # bttv_emotes has always mixed the globals in; older consumers rely on that.
    bttv = global_emotes["bttv"] + channel["bttv"]
    return {
        "ffz_emotes": channel["ffz"],
        "bttv_emotes": bttv,
        "seventv_emotes": channel["7tv"],
        "global_emotes": global_emotes,
    }


def merge_emotes(existing: Emote | None, fetched: dict[str, Any], *, force: bool, now: dt.datetime) -> dict[str, Any]:
    """Attribute values to write for a VOD's emotes row.

    A new row (or ``force``) takes everything fetched, globals marked 'captured'.
    An existing row keeps what it has, so a re-run never swaps a historical set
    for the current one: only empty channel sets and empty global providers are
    filled, and globals filled in later are marked 'backfilled'.
    """
    if existing is None or force:
        return {
            **{k: fetched[k] for k in CHANNEL_SETS},
            "global_emotes": fetched["global_emotes"],
            "global_emotes_source": "captured",
            "global_emotes_at": now,
        }
    values = {k: fetched[k] for k in CHANNEL_SETS if not getattr(existing, k) and fetched[k]}
    old = existing.global_emotes or {}
    missing = {
        p: fetched["global_emotes"][p] for p in providers.PROVIDERS if not old.get(p) and fetched["global_emotes"][p]
    }
    if missing:
        values.update(global_emotes={**old, **missing}, global_emotes_source="backfilled", global_emotes_at=now)
    return values


async def emotes(ctx: JobContext) -> None:
    """Save the channel and global emote sets. ``payload.force`` overwrites an existing row."""
    vod_id = ctx.require_vod_id()
    await ctx.refuse_if_spliced()  # a merged VOD holds both VODs' sets
    force = bool(ctx.payload.get("force"))
    fetched = await fetch_emotes(ctx, ctx.settings.twitch_id)
    async with get_sessionmaker()() as s:
        existing = (await s.execute(select(Emote).where(Emote.vod_id == vod_id).with_for_update())).scalar_one_or_none()
        values = merge_emotes(existing, fetched, force=force, now=dt.datetime.now(dt.UTC))
        if existing is None:
            s.add(Emote(vod_id=vod_id, **values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        await s.commit()
    g = fetched["global_emotes"]
    ctx.log.info(
        "emotes: ffz=%d bttv=%d 7tv=%d, globals 7tv=%d bttv=%d ffz=%d",
        len(fetched["ffz_emotes"]),
        len(fetched["bttv_emotes"]),
        len(fetched["seventv_emotes"]),
        len(g["7tv"]),
        len(g["bttv"]),
        len(g["ffz"]),
    )
    if existing is not None and not force:
        ctx.log.info("emotes row existed; filled %s (pass force to overwrite)", ", ".join(values) or "nothing")


async def global_emotes_backfill(ctx: JobContext) -> None:
    """Give every emotes row without global sets the current ones, marked 'backfilled'.

    Idempotent: rows that already have ``global_emotes`` are skipped, and the
    channel columns are never touched. ``payload.vod_ids`` limits it to those VODs.
    """
    global_emotes = await fetch_global_emotes(ctx)
    failed = [p for p, v in global_emotes.items() if not v]
    if failed:
        raise StepError(f"could not fetch the {', '.join(failed)} global emotes; nothing backfilled")
    stmt = (
        update(Emote)
        .where(Emote.global_emotes.is_(None))
        .values(global_emotes=global_emotes, global_emotes_source="backfilled", global_emotes_at=func.now())
    )
    if ctx.payload.get("vod_ids"):
        stmt = stmt.where(Emote.vod_id.in_([str(v) for v in ctx.payload["vod_ids"]]))
    async with get_sessionmaker()() as s:
        count = (await s.execute(stmt)).rowcount
        await s.commit()
    ctx.log.info("backfilled global emotes on %d row(s)", count)


# ── 7TV zero-width flags on sets saved before ``flags`` was kept ─────────────

SEVENTV_CONCURRENCY = 5


def apply_flags(entries: list[Any] | None, emote_flags: dict[str, int]) -> list[Any] | None:
    """``entries`` with ``flags`` added where it is missing, or None if nothing changed.

    ``emote_flags`` maps an emote id to 7TV's own ``data.flags``. The entry gets the
    set-entry form new captures have (``flags``: 1 = zero-width) and keeps 7TV's value
    as ``data_flags``. Ids 7TV no longer knows are left without ``flags``.
    """
    out, changed = [], False
    for e in entries or []:
        emote = emote_flags.get(str(e.get("id"))) if isinstance(e, dict) and "flags" not in e else None
        if emote is None:
            out.append(e)
            continue
        zero_width = emote & providers.SEVENTV_EMOTE_ZERO_WIDTH
        out.append({**e, "flags": providers.SEVENTV_ENTRY_ZERO_WIDTH if zero_width else 0, "data_flags": emote})
        changed = True
    return out if changed else None


def _missing_flag_ids(entries: list[Any] | None) -> set[str]:
    return {str(e["id"]) for e in entries or [] if isinstance(e, dict) and e.get("id") is not None and "flags" not in e}


async def _seventv_emote_flags(ids: set[str], ctx: JobContext) -> dict[str, int]:
    """7TV's ``flags`` for each id it knows. Unknown ids and failed requests are left out."""
    sem = asyncio.Semaphore(SEVENTV_CONCURRENCY)
    unknown: list[str] = []

    async def one(emote_id: str) -> tuple[str, int | None]:
        async with sem:
            try:
                data = (await http.request("GET", providers.seventv_emote(emote_id))).json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (400, 404):
                    unknown.append(emote_id)
                else:
                    ctx.log.warning("7TV emote %s: %s", emote_id, exc)
                return emote_id, None
            except Exception as exc:
                ctx.log.warning("7TV emote %s: %s", emote_id, exc)
                return emote_id, None
        flags = data.get("flags") if isinstance(data, dict) else None
        return emote_id, flags if isinstance(flags, int) else None

    results = dict(await asyncio.gather(*(one(i) for i in sorted(ids))))
    if unknown:
        ctx.log.info("7TV no longer knows %d emote(s); they keep no flags", len(unknown))
    return {i: f for i, f in results.items() if f is not None}


async def seventv_flags_backfill(ctx: JobContext) -> None:
    """Add 7TV's zero-width flags to saved channel sets from before ``flags`` was kept.

    Each distinct emote id is looked up once (``GET /v3/emotes/{id}``). Only entries
    without ``flags`` change, so a re-run retries just the ones that failed.
    ``payload.vod_ids`` limits it to those VODs. The globals were always saved with flags.
    Undoing a merge or split made before this ran restores its snapshot, flags-less; run it again after.
    """
    stmt = select(Emote.vod_id, Emote.seventv_emotes)
    if ctx.payload.get("vod_ids"):
        stmt = stmt.where(Emote.vod_id.in_([str(v) for v in ctx.payload["vod_ids"]]))
    async with get_sessionmaker()() as s:
        missing = {
            vod_id: ids for vod_id, entries in (await s.execute(stmt)).all() if (ids := _missing_flag_ids(entries))
        }
    ids = set().union(*missing.values())
    if not ids:
        ctx.log.info("every saved 7TV set already has flags")
        return
    ctx.log.info("looking up %d 7TV emote(s) for %d VOD(s)", len(ids), len(missing))
    emote_flags = await _seventv_emote_flags(ids, ctx)

    updated = 0
    for vod_id in sorted(missing):
        async with get_sessionmaker()() as s:
            row = (await s.execute(select(Emote).where(Emote.vod_id == vod_id).with_for_update())).scalar_one_or_none()
            entries = apply_flags(row.seventv_emotes, emote_flags) if row is not None else None
            if entries is not None:
                row.seventv_emotes = entries
                await notify_rows_moved(s, vod_id)
                updated += 1
            await s.commit()
    ctx.log.info("added 7TV flags on %d row(s) (%d of %d emotes known to 7TV)", updated, len(emote_flags), len(ids))
