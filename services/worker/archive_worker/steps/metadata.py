"""Metadata steps: chapters, chat replay, third-party emotes, manual chat import."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from archive_common import http
from archive_common.db import get_sessionmaker
from archive_common.models import Emote, Log

from .. import planning
from ..context import JobContext, StepError

CHAT_BATCH = 2500
DEFAULT_COLOR = "#999999"


def hhmmss_to_seconds(value: str | None) -> int:
    total = 0
    for piece in (value or "0").split(":"):
        total = total * 60 + int(float(piece or 0))
    return total


async def vod_duration(ctx: JobContext) -> float:
    vod = await ctx.get_vod()
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


def _parse_ts(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


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
        "created_at": _parse_ts(node.get("createdAt")),
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


async def fetch_emotes(ctx: JobContext, twitch_id: str) -> dict[str, list[dict]]:
    ffz_data, bttv_global, bttv_user, stv = await asyncio.gather(
        _json(f"{FFZ}/room/id/{twitch_id}", ctx),
        _json(f"{BTTV}/cached/emotes/global", ctx),
        _json(f"{BTTV}/cached/users/twitch/{twitch_id}", ctx),
        _json(f"{SEVENTV}/users/twitch/{twitch_id}", ctx),
    )
    ffz: list[dict] = []
    if ffz_data:
        room_set = str(ffz_data["room"]["set"])
        ffz = [{"id": e["id"], "code": e["name"]} for e in ffz_data["sets"][room_set]["emoticons"]]
    bttv = [{"id": e["id"], "code": e["code"]} for e in bttv_global or []]
    if bttv_user:
        user_emotes = (bttv_user.get("channelEmotes") or []) + (bttv_user.get("sharedEmotes") or [])
        bttv += [{"id": e["id"], "code": e["code"]} for e in user_emotes]
    seventv = []
    if stv and stv.get("emote_set"):
        seventv = [{"id": e["id"], "code": e["name"], "flags": e.get("flags")} for e in stv["emote_set"]["emotes"]]
    return {"ffz_emotes": ffz, "bttv_emotes": bttv, "seventv_emotes": seventv}


async def emotes(ctx: JobContext) -> None:
    vod_id = ctx.require_vod_id()
    values = await fetch_emotes(ctx, ctx.settings.twitch_id)
    async with get_sessionmaker()() as s:
        stmt = insert(Emote).values(vod_id=vod_id, **values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Emote.vod_id],
            set_={
                "ffz_emotes": stmt.excluded.ffz_emotes,
                "bttv_emotes": stmt.excluded.bttv_emotes,
                "7tv_emotes": stmt.excluded["7tv_emotes"],
                "updatedAt": func.now(),
            },
        )
        await s.execute(stmt)
        await s.commit()
    ctx.log.info(
        "emotes: ffz=%d bttv=%d 7tv=%d",
        len(values["ffz_emotes"]), len(values["bttv_emotes"]), len(values["seventv_emotes"]),
    )
