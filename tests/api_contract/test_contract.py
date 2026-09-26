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


# Fields added for the new sites. Legacy responses never had them; everything else must match.
ADDED_VOD_FIELDS = {"duration_seconds", "merged_into"}  # merged_into: only on merged-away VODs
ADDED_CHAPTER_FIELDS = {"imageTemplate", "length"}
# Global emote sets saved with each VOD (emotes rows are the dicts with "7tv_emotes").
ADDED_EMOTE_FIELDS = {"global_emotes", "global_emotes_source", "global_emotes_at"}


def _without_additions(node):
    """``node`` with the added vod/chapter fields removed (vods may be nested, e.g. games[].vod)."""
    if isinstance(node, list):
        return [_without_additions(v) for v in node]
    if not isinstance(node, dict):
        return node
    added = ADDED_EMOTE_FIELDS if "7tv_emotes" in node else ADDED_VOD_FIELDS
    out = {k: _without_additions(v) for k, v in node.items() if k not in added}
    if isinstance(node.get("chapters"), list):
        out["chapters"] = [
            {k: v for k, v in c.items() if k not in ADDED_CHAPTER_FIELDS} if isinstance(c, dict) else c
            for c in node["chapters"]
        ]
    return out


def test_golden_has_no_added_fields() -> None:
    assert _without_additions(GOLDEN) == GOLDEN


def _is_unordered_list(path: str) -> bool:
    return "$sort" not in urlsplit(path).query


@pytest.mark.parametrize("entry", GOLDEN, ids=[e["path"][:90] for e in GOLDEN])
async def test_golden(client: httpx.AsyncClient, entry: dict) -> None:
    path = entry["path"]
    if path in KNOWN_DIFFERENCES:
        pytest.skip("intentional deviation from legacy behaviour")
    resp = await client.get(path)
    assert resp.status_code == entry["status"], resp.text[:500]
    body = resp.json()
    if resp.status_code < 400 and urlsplit(path).path.startswith("/emotes"):
        for item in body.get("data", [body]):
            assert ADDED_EMOTE_FIELDS <= set(item), f"{path}: missing {ADDED_EMOTE_FIELDS - set(item)}"
    body = _without_additions(body)
    expected = entry["body"]

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
