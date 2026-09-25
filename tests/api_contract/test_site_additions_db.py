"""Endpoints and fields added for the new sites (not in the legacy API).

Like test_contract.py, these run against the local Postgres and skip without it.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from archive_api.serialize import box_art_template


async def _all_vods(client: httpx.AsyncClient, query: str = "") -> list[dict]:
    vods: list[dict] = []
    while True:
        page = (await client.get(f"/vods?$limit=50&$skip={len(vods)}&$sort[id]=1{query}")).json()
        vods += page["data"]
        if len(vods) >= page["total"] or not page["data"]:
            return vods


async def test_games_played_matches_the_client_side_computation(client: httpx.AsyncClient) -> None:
    """The same list the site used to build by paging through /vods."""
    expected: dict[str, dict] = {}
    for vod in sorted(await _all_vods(client), key=lambda v: v["createdAt"]):
        for ch in vod["chapters"] or []:
            key = "none" if ch.get("name") is None else f"id:{ch['gameId']}" if ch.get("gameId") else f"n:{ch['name']}"
            e = expected.setdefault(key, {"vods": set(), "chapters": 0})
            e["vods"].add(vod["id"])
            e["chapters"] += 1
            e["lastPlayed"] = vod["createdAt"]
            e["name"] = ch.get("name") or "No category"
            e["gameId"] = None if key == "none" else ch.get("gameId")
            if ch.get("image"):
                e["image"] = ch["image"]

    resp = await client.get("/v1/games-played")
    assert resp.status_code == 200
    got = resp.json()
    assert len(got) == len(expected)
    by_key = {("none" if g["gameId"] is None and g["name"] == "No category" else
               f"id:{g['gameId']}" if g["gameId"] else f"n:{g['name']}"): g for g in got}
    for key, e in expected.items():
        g = by_key[key]
        assert g["vods"] == len(e["vods"]), key
        assert g["chapters"] == e["chapters"], key
        assert g["lastPlayed"] == e["lastPlayed"], key
        assert g["gameId"] == e["gameId"], key
        assert g["name"] == e["name"], key
        assert g["image"] == e.get("image"), key
        assert g["imageTemplate"] == box_art_template(g["image"])

    # vods desc, then lastPlayed desc (name order is the database collation's)
    for a, b in zip(got, got[1:]):
        assert (a["vods"], a["lastPlayed"]) >= (b["vods"], b["lastPlayed"])


async def test_game_id_filter_is_exact(client: httpx.AsyncClient) -> None:
    games = (await client.get("/v1/games-played")).json()
    game = next(g for g in games if g["gameId"])
    resp = await client.get(f"/vods?chapters[gameId]={game['gameId']}&$limit=50")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == game["vods"]
    assert all(any(c.get("gameId") == game["gameId"] for c in v["chapters"]) for v in body["data"])


async def test_name_eq_filter_is_exact_and_case_sensitive(client: httpx.AsyncClient) -> None:
    games = (await client.get("/v1/games-played")).json()
    game = next(g for g in games if g["gameId"])
    name = quote(game["name"])
    exact = (await client.get(f"/vods?chapters[name][$eq]={name}&$limit=1")).json()["total"]
    substring = (await client.get(f"/vods?chapters[name]={name}&$limit=1")).json()["total"]
    assert exact == game["vods"]
    assert substring >= exact
    assert (await client.get(f"/vods?chapters[name][$eq]={quote(game['name'].swapcase())}&$limit=1")).json()[
        "total"
    ] == 0
    assert (await client.get(f"/vods?chapters[name][$eq]={name[:-1]}&$limit=1")).json()["total"] == 0


async def test_uncategorised_filter(client: httpx.AsyncClient) -> None:
    resp = await client.get("/vods?chapters[gameId]=null&$limit=50")
    assert resp.status_code == 200
    body = resp.json()
    none = next((g for g in (await client.get("/v1/games-played")).json() if g["name"] == "No category"), None)
    assert body["total"] == (none["vods"] if none else 0)
    assert all(any(c.get("gameId") is None for c in v["chapters"]) for v in body["data"])


async def test_chapter_filter_values_are_not_interpolated(client: httpx.AsyncClient) -> None:
    for q in ('chapters[gameId]=1") || (true', 'chapters[name][$eq]=") || @.name like_regex ".*'):
        resp = await client.get(f"/vods?{quote(q, safe='=[]$')}&$limit=1")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


async def test_bad_chapter_filters_are_400(client: httpx.AsyncClient) -> None:
    for q in ("chapters[foo]=1", "chapters[name][$ne]=x", "chapters[gameId]=", "chapters[gameId][$in][]=1"):
        assert (await client.get(f"/vods?{q}")).status_code == 400, q


async def test_vods_carry_added_fields(client: httpx.AsyncClient) -> None:
    vod = (await client.get("/vods?$limit=1&$sort[createdAt]=-1")).json()["data"][0]
    h, m, s = (int(x) for x in vod["duration"].split(":"))
    assert vod["duration_seconds"] == h * 3600 + m * 60 + s
    for ch in vod["chapters"]:
        assert ch["length"] == ch["end"]
        assert ch["imageTemplate"] == box_art_template(ch["image"])
        if ch["image"]:
            size = ch["imageTemplate"].replace("{width}x{height}", "40x53")
            assert size == ch["image"]


async def test_status(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"live", "stream", "vod"}
    latest = (await client.get("/vods?$limit=1&$sort[createdAt]=-1")).json()["data"]
    if body["live"]:
        assert set(body["stream"]) == {"id", "started_at", "title", "game"}
        assert body["vod"] is None or body["vod"]["stream_id"] == body["stream"]["id"]
    else:
        assert body["stream"] is None
        assert body["vod"] == (latest[0] if latest else None)
