"""GET /v1/status: the live stream (if any) plus its VOD, or the latest VOD when offline.

Whether the channel is live comes from the ``streams`` table the worker keeps
up to date. The live title and category come from Helix when it is configured,
else from the VOD row (its title and last chapter).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from archive_common.serialize import STREAMS, VODS, box_art_image, box_art_template, vods_json
from archive_common.twitch.helix import Helix


log = logging.getLogger(__name__)


async def _latest_vod(conn: AsyncConnection, stream_id: str | None = None) -> dict | None:
    where = [VODS.table.c.stream_id == stream_id] if stream_id is not None else []
    vods = await vods_json(conn, *where, order_by=VODS.table.c.createdAt.desc(), limit=1)
    return vods[0] if vods else None


def _game(name: str | None, game_id: str | None, image: str | None) -> dict | None:
    if not name and not game_id:
        return None
    return {"name": name, "gameId": game_id, "image": image, "imageTemplate": box_art_template(image)}


async def _helix_stream(helix: Helix, twitch_id: str, stream_id: str) -> dict | None:
    """{title, game} of the live stream from Helix, or None if unavailable."""
    if not helix.configured or not twitch_id:
        return None
    try:
        live = await helix.get_stream(twitch_id)
        if not live or str(live.get("id")) != stream_id:
            return None
        game_id = live.get("game_id") or None
        image = None
        if game_id:
            box_art = ((await helix.get_game(game_id)) or {}).get("box_art_url")
            image = box_art_image(box_art)
        return {"title": live.get("title"), "game": _game(live.get("game_name") or None, game_id, image)}
    except httpx.HTTPError as exc:
        log.warning("failed to fetch the live stream from Helix: %s", exc)
        return None


async def stream_status(conn: AsyncConnection, helix: Helix, twitch_id: str) -> dict[str, Any]:
    stmt = (
        select(*STREAMS.columns())
        .where(STREAMS.table.c.is_live.is_(True))
        .order_by(STREAMS.table.c.started_at.desc().nulls_last())
        .limit(1)
    )
    row = (await conn.execute(stmt)).mappings().first()
    if row is None:
        return {"live": False, "stream": None, "vod": await _latest_vod(conn)}

    live = STREAMS.to_json(row)
    vod, info = await asyncio.gather(_latest_vod(conn, live["id"]), _helix_stream(helix, twitch_id, live["id"]))
    if info is None:
        last = next((c for c in reversed((vod or {}).get("chapters") or []) if isinstance(c, dict)), {})
        info = {
            "title": (vod or {}).get("title"),
            "game": _game(last.get("name"), last.get("gameId"), last.get("image")),
        }
    stream = {"id": live["id"], "started_at": live["started_at"], **info}
    return {"live": True, "stream": stream, "vod": vod}
