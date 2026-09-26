"""GET /v1/games-played: one entry per distinct game across all VODs' chapters."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from archive_common.serialize import box_art_template, js_iso

NO_CATEGORY = "No category"

# Chapters are grouped by gameId, else by name; chapters with no name (no Twitch
# category) form a single group. Within a group the name, gameId and image come
# from the most recent chapter that has one (games get renamed on Twitch).
# A chapter's ``end`` is its length in seconds (not its end time); one that is
# missing or not a JSON number counts as 0. A merge's gap chapters (kind "gap") are
# no game, and a VOD merged into another is counted once, as part of that one.
_SQL = text(
    """
    with chapters as (
        select v.id as vod_id, v."createdAt" as created_at, c.ord,
               c.value ->> 'name' as name,
               nullif(c.value ->> 'gameId', '') as game_id,
               nullif(c.value ->> 'image', '') as image,
               case when jsonb_typeof(c.value -> 'end') = 'number'
                   then (c.value ->> 'end')::numeric else 0 end as length_s,
               c.value @> '{"restricted": true}' as restricted
        from vods v
        cross join lateral jsonb_array_elements(
            case when jsonb_typeof(v.chapters) = 'array' then v.chapters else '[]'::jsonb end
        ) with ordinality as c(value, ord)
        where jsonb_typeof(c.value) = 'object'
          and not c.value @> '{"kind": "gap"}'
          and v.merged_into is null
    ),
    keyed as (
        select *,
               case
                   when name is null then 'none'
                   when game_id is not null then 'id:' || game_id
                   else 'name:' || name
               end as key
        from chapters
    ),
    grouped as (
        select
            (array_agg(name order by created_at desc, ord desc) filter (where name is not null))[1] as name,
            (array_agg(game_id order by created_at desc, ord desc)
                filter (where game_id is not null and key <> 'none'))[1] as game_id,
            (array_agg(image order by created_at desc, ord desc) filter (where image is not null))[1] as image,
            count(distinct vod_id) as vods,
            count(*) as chapters,
            max(created_at) as last_played,
            sum(length_s) as seconds,
            sum(case when restricted then 0 else length_s end) as watchable_seconds
        from keyed
        group by key
    )
    select coalesce(name, :no_category) as name, game_id, image, vods, chapters, last_played,
           seconds, watchable_seconds
    from grouped
    order by vods desc, last_played desc, coalesce(name, :no_category)
    """
)


async def games_played(conn: AsyncConnection) -> list[dict[str, Any]]:
    rows = await conn.execute(_SQL, {"no_category": NO_CATEGORY})
    return [
        {
            "name": r["name"],
            "gameId": r["game_id"],
            "image": r["image"],
            "imageTemplate": box_art_template(r["image"]),
            "vods": r["vods"],
            "chapters": r["chapters"],
            "lastPlayed": js_iso(r["last_played"]),
            "seconds": round(r["seconds"]),
            "watchableSeconds": round(r["watchable_seconds"]),
        }
        for r in rows.mappings()
    ]
