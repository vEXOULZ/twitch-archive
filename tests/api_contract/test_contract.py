"""Replay golden responses captured from the legacy API against archive-api.

Needs a Postgres restored from the same data the golden file was captured from
(see README "Development"). Set ARCHIVE_DATABASE_URL; the test is skipped when
the database is unreachable.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

GOLDEN = json.loads((Path(__file__).parent / "golden.json").read_text(encoding="utf-8"))

# Intentional deviations from the legacy API (legacy behaviour was a bug).
KNOWN_DIFFERENCES = {
    # legacy crashed with 500 "column vods.createdAt does not exist" on any $select
    "/vods?$select[]=id&$select[]=title&$limit=5&$sort[createdAt]=-1",
}


# Fields added after the golden capture, per route. Legacy consumers ignore them;
# the replay checks they are present, then compares the rest field for field.
ADDED_FIELDS = {
    "/emotes": {"global_emotes", "global_emotes_source", "global_emotes_at"},
}


def _strip_added(path: str, body):
    added = next((f for prefix, f in ADDED_FIELDS.items() if urlsplit(path).path.startswith(prefix)), None)
    if not added or not isinstance(body, dict):
        return body
    items = body["data"] if "data" in body else [body]
    for item in items:
        assert added <= set(item), f"{path}: missing {added - set(item)}"
        for key in added:
            del item[key]
    return body


def _is_unordered_list(path: str) -> bool:
    return "$sort" not in urlsplit(path).query


@pytest.fixture(scope="module")
async def client():
    from archive_common.config import Settings, get_settings

    get_settings.cache_clear()
    settings = get_settings()
    settings.rate_limit_points = 1_000_000
    try:
        import sqlalchemy
        from archive_common.db import get_engine

        async with get_engine().connect() as conn:
            await conn.execute(sqlalchemy.text("select 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")

    from archive_api.main import create_app

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    assert isinstance(settings, Settings)


@pytest.mark.parametrize("entry", GOLDEN, ids=[e["path"][:90] for e in GOLDEN])
async def test_golden(client: httpx.AsyncClient, entry: dict) -> None:
    path = entry["path"]
    if path in KNOWN_DIFFERENCES:
        pytest.skip("intentional deviation from legacy behaviour")
    resp = await client.get(path)
    assert resp.status_code == entry["status"], resp.text[:500]
    body = resp.json()
    expected = entry["body"]
    if resp.status_code < 400:
        body = _strip_added(path, body)

    if resp.status_code >= 400:
        # Legacy error bodies: compare the fields the frontend can observe.
        if isinstance(expected, dict) and "error" in expected:
            assert body == expected
        else:
            assert body["name"] == expected["name"] and body["code"] == expected["code"]
        return

    if isinstance(expected, dict) and "data" in expected and _is_unordered_list(path):
        # No $sort: row order is whatever Postgres returns; compare the envelope.
        assert {k: body[k] for k in ("total", "limit", "skip")} == {
            k: expected[k] for k in ("total", "limit", "skip")
        }
        assert len(body["data"]) == len(expected["data"])
        if expected["total"] <= expected["limit"]:
            key = "id" if expected["data"] and "id" in expected["data"][0] else "vodId"
            assert sorted(json.dumps(d, sort_keys=True) for d in body["data"]) == sorted(
                json.dumps(d, sort_keys=True) for d in expected["data"]
            ), key
        return

    assert body == expected


async def test_select_returns_only_selected_fields(client: httpx.AsyncClient) -> None:
    resp = await client.get("/vods?$select[]=id&$select[]=title&$limit=5&$sort[createdAt]=-1")
    assert resp.status_code == 200
    item = resp.json()["data"][0]
    assert set(item) == {"id", "title", "games"}


async def test_chapter_filter_escapes_regex(client: httpx.AsyncClient) -> None:
    # Legacy interpolated this into a regex; "(" made Postgres error out.
    resp = await client.get("/vods?chapters[name]=(&$limit=5")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


async def test_chapter_filter_combines_with_other_filters(client: httpx.AsyncClient) -> None:
    all_jc = (await client.get("/vods?chapters[name]=Just%20Chatting&$limit=50")).json()["total"]
    some = (
        await client.get("/vods?chapters[name]=Just%20Chatting&createdAt[$gte]=2025-06-01T00:00:00.000Z&$limit=50")
    ).json()["total"]
    assert 0 < some < all_jc


async def test_writes_are_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.post("/vods", json={"id": "x"})
    assert resp.status_code == 405
    assert resp.json()["className"] == "method-not-allowed"


async def test_bad_filter_value_is_400(client: httpx.AsyncClient) -> None:
    resp = await client.get("/vods?createdAt[$gte]=not-a-date")
    assert resp.status_code == 400


async def test_unknown_filter_is_400(client: httpx.AsyncClient) -> None:
    resp = await client.get("/vods?nope=1")
    assert resp.status_code == 400


def test_golden_has_cursor_pages() -> None:
    cursors = [e for e in GOLDEN if "cursor=" in e["path"] and e["status"] == 200]
    assert cursors, "golden file should exercise cursor paging"
    for e in cursors:
        assert parse_qs(urlsplit(e["path"]).query)["cursor"]
