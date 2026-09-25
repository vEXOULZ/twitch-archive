"""Metadata steps: chapters, chat replay, third-party emotes, manual chat import."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from archive_common import http
from archive_common.db import get_sessionmaker
from archive_common.models import Emote, Log, Vod
from archive_common.timeutil import hhmmss_to_seconds, parse_ts

from .. import planning
from ..context import JobContext, StepError

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
    duration = await vod_duration(ctx)
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


def comment_row(vod_id: str, node: dict) -> dict:
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
        "created_at": parse_ts(node.get("createdAt")) or dt.datetime.now(dt.timezone.utc),
    }


async def insert_comments(rows: list[dict]) -> None:
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
    gql = ctx.deps.gql
    async with get_sessionmaker()() as s:
        offset = (
            await s.execute(select(func.max(Log.content_offset_seconds)).where(Log.vod_id == vod_id))
        ).scalar() or 0

    video = await gql.comments(vod_id, offset=offset)
    rows: list[dict] = []
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
    data = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    edges = ((data.get("comments") or {}).get("edges")) or []
    rows = [comment_row(vod_id, e["node"]) for e in edges if e.get("node")]
    await insert_comments(rows)
    ctx.log.info("imported %d comment(s) from %s", len(rows), path)


# ── Emotes ────────────────────────────────────────────────────────────────

FFZ = "https://api.frankerfacez.com/v1"
BTTV = "https://api.betterttv.net/3"
SEVENTV = "https://7tv.io/v3"


async def _json(url: str, ctx: JobContext):
    try:
        return (await http.request("GET", url)).json()
    except Exception as exc:
        ctx.log.warning("emote fetch %s failed: %s", url, exc)
        return None


def _7tv_emotes(emote_set: dict) -> list[dict]:
    return [{"id": e["id"], "code": e["name"], "flags": e.get("flags")} for e in emote_set.get("emotes") or []]


def _bttv_emotes(emotes: list) -> list[dict]:
    return [{"id": e["id"], "code": e["code"]} for e in emotes]


def _ffz_global_emotes(data: dict) -> list[dict]:
    sets = data["sets"]
    return [{"id": e["id"], "code": e["name"]} for sid in data["default_sets"] for e in sets[str(sid)]["emoticons"]]


GLOBAL_PROVIDERS = ("7tv", "bttv", "ffz")


async def fetch_global_emotes(ctx: JobContext) -> dict[str, list[dict]]:
    """The providers' current global sets. A provider that fails gets an empty list."""
    responses = await asyncio.gather(
        _json(f"{SEVENTV}/emote-sets/global", ctx),
        _json(f"{BTTV}/cached/emotes/global", ctx),
        _json(f"{FFZ}/set/global", ctx),
    )
    out: dict[str, list[dict]] = {}
    for provider, parse, data in zip(GLOBAL_PROVIDERS, (_7tv_emotes, _bttv_emotes, _ffz_global_emotes), responses):
        out[provider] = []
        if data is None:
            continue
        try:
            out[provider] = parse(data)
        except Exception as exc:
            ctx.log.warning("global %s emotes: unexpected response: %r", provider, exc)
    return out


async def fetch_emotes(ctx: JobContext, twitch_id: str) -> dict:
    ffz_data, bttv_user, stv, global_emotes = await asyncio.gather(
        _json(f"{FFZ}/room/id/{twitch_id}", ctx),
        _json(f"{BTTV}/cached/users/twitch/{twitch_id}", ctx),
        _json(f"{SEVENTV}/users/twitch/{twitch_id}", ctx),
        fetch_global_emotes(ctx),
    )
    ffz: list[dict] = []
    if ffz_data:
        room_set = str(ffz_data["room"]["set"])
        ffz = [{"id": e["id"], "code": e["name"]} for e in ffz_data["sets"][room_set]["emoticons"]]
    # bttv_emotes has always mixed the globals in; older consumers rely on that.
    bttv = list(global_emotes["bttv"])
    if bttv_user:
        bttv += _bttv_emotes((bttv_user.get("channelEmotes") or []) + (bttv_user.get("sharedEmotes") or []))
    seventv = []
    if stv and stv.get("emote_set"):
        seventv = _7tv_emotes(stv["emote_set"])
    return {"ffz_emotes": ffz, "bttv_emotes": bttv, "seventv_emotes": seventv, "global_emotes": global_emotes}


CHANNEL_SETS = ("ffz_emotes", "bttv_emotes", "seventv_emotes")


def merge_emotes(existing: Emote | None, fetched: dict, *, force: bool, now: dt.datetime) -> dict:
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
    missing = {p: fetched["global_emotes"][p] for p in GLOBAL_PROVIDERS if not old.get(p) and fetched["global_emotes"][p]}
    if missing:
        values.update(global_emotes={**old, **missing}, global_emotes_source="backfilled", global_emotes_at=now)
    return values


async def emotes(ctx: JobContext) -> None:
    """Save the channel and global emote sets. ``payload.force`` overwrites an existing row."""
    vod_id = ctx.require_vod_id()
    force = bool(ctx.payload.get("force"))
    fetched = await fetch_emotes(ctx, ctx.settings.twitch_id)
    async with get_sessionmaker()() as s:
        existing = (
            await s.execute(select(Emote).where(Emote.vod_id == vod_id).with_for_update())
        ).scalar_one_or_none()
        values = merge_emotes(existing, fetched, force=force, now=dt.datetime.now(dt.timezone.utc))
        if existing is None:
            s.add(Emote(vod_id=vod_id, **values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        await s.commit()
    g = fetched["global_emotes"]
    ctx.log.info(
        "emotes: ffz=%d bttv=%d 7tv=%d, globals 7tv=%d bttv=%d ffz=%d",
        len(fetched["ffz_emotes"]), len(fetched["bttv_emotes"]), len(fetched["seventv_emotes"]),
        len(g["7tv"]), len(g["bttv"]), len(g["ffz"]),
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
