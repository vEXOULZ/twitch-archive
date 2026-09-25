"""Fields and endpoints added for the new sites that need no database."""

from __future__ import annotations

import httpx
import pytest
import respx

from archive_api import third_party_emotes as tpe
from archive_api.main import create_app
from archive_api.serialize import box_art_template, chapter_additions, duration_seconds, vod_additions
from archive_api.status import _helix_stream
from archive_common.config import Settings

TWITCH_ID = "38656648"


# ── Box art / durations ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "template"),
    [
        (
            "https://static-cdn.jtvnw.net/ttv-boxart/509658-40x53.jpg",
            "https://static-cdn.jtvnw.net/ttv-boxart/509658-{width}x{height}.jpg",
        ),
        (
            "https://static-cdn.jtvnw.net/ttv-boxart/1648333914_IGDB-40x53.jpg",
            "https://static-cdn.jtvnw.net/ttv-boxart/1648333914_IGDB-{width}x{height}.jpg",
        ),
        (
            "https://static-cdn.jtvnw.net/ttv-boxart/1-285x380.png",
            "https://static-cdn.jtvnw.net/ttv-boxart/1-{width}x{height}.png",
        ),
        # already a template, or not sized: unchanged
        ("https://x/ttv-boxart/1-{width}x{height}.jpg", "https://x/ttv-boxart/1-{width}x{height}.jpg"),
        ("https://x/art.jpg", "https://x/art.jpg"),
        (None, None),
        ("", None),
    ],
)
def test_box_art_template(url, template) -> None:
    assert box_art_template(url) == template


@pytest.mark.parametrize(
    ("value", "seconds"), [("06:56:40", 25000), ("31:10", 1870), ("00:00:00", 0), ("120:00:01", 432001), (None, None)]
)
def test_duration_seconds(value, seconds) -> None:
    assert duration_seconds(value) == seconds


def test_chapter_additions_keep_legacy_fields() -> None:
    ch = {"end": 2110, "name": "Just Chatting", "image": "https://x/509658-40x53.jpg", "start": 0, "gameId": "509658"}
    out = chapter_additions(ch)
    assert {k: out[k] for k in ch} == ch
    assert out["length"] == 2110
    assert out["imageTemplate"] == "https://x/509658-{width}x{height}.jpg"
    assert list(out)[: len(ch)] == list(ch)  # legacy keys first, in their order
    assert "length" not in ch  # stored row not mutated
    assert chapter_additions({"name": None, "image": None, "end": 5})["imageTemplate"] is None


def test_vod_additions_follow_select() -> None:
    vod = {"id": "1", "title": "t"}
    vod_additions(vod)
    assert vod == {"id": "1", "title": "t"}
    vod = {"id": "1", "duration": "01:00:00", "chapters": [{"end": 3600, "image": None}]}
    vod_additions(vod)
    assert vod["duration_seconds"] == 3600
    assert vod["chapters"][0]["length"] == 3600


# ── Third-party emotes ────────────────────────────────────────────────────


def _mock_providers(router: respx.MockRouter, **overrides) -> None:
    routes = {
        "7tv_global": (f"{tpe.SEVENTV}/emote-sets/global", {"emotes": [{"id": "g1", "name": "EZ"}, {"id": "g2", "name": "Clap"}]}),
        "7tv_channel": (
            f"{tpe.SEVENTV}/users/twitch/{TWITCH_ID}",
            {"emote_set": {"emotes": [{"id": "c1", "name": "vexHi"}, {"id": "c2", "name": "EZ"}]}},
        ),
        "bttv_global": (f"{tpe.BTTV}/cached/emotes/global", [{"id": "b1", "code": "monkaS"}]),
        "bttv_channel": (
            f"{tpe.BTTV}/cached/users/twitch/{TWITCH_ID}",
            {"channelEmotes": [{"id": "b2", "code": "catJAM"}], "sharedEmotes": [{"id": "b3", "code": "pepeD"}]},
        ),
        "ffz_global": (
            f"{tpe.FFZ}/set/global",
            {"default_sets": [3], "sets": {"3": {"emoticons": [{"id": 25927, "name": "CatBag"}]},
                                          "4330": {"emoticons": [{"id": 1, "name": "NotDefault"}]}}},
        ),
        "ffz_channel": (
            f"{tpe.FFZ}/room/id/{TWITCH_ID}",
            {"room": {"set": 123}, "sets": {"123": {"emoticons": [{"id": 99, "name": "vexLUL"}]}}},
        ),
    }
    for name, (url, body) in routes.items():
        response = overrides.get(name, httpx.Response(200, json=body))
        router.get(url).mock(return_value=response)


@respx.mock(assert_all_called=True)
async def test_third_party_emotes(respx_mock: respx.MockRouter) -> None:
    _mock_providers(respx_mock)
    out = await tpe.fetch_third_party_emotes(TWITCH_ID)
    assert out["failed"] == []
    # the channel's "EZ" replaces the global one
    assert out["7tv"] == [
        {"id": "c2", "code": "EZ", "provider": "7tv"},
        {"id": "g2", "code": "Clap", "provider": "7tv"},
        {"id": "c1", "code": "vexHi", "provider": "7tv"},
    ]
    assert [e["code"] for e in out["bttv"]] == ["monkaS", "catJAM", "pepeD"]
    assert out["ffz"] == [
        {"id": "25927", "code": "CatBag", "provider": "ffz"},
        {"id": "99", "code": "vexLUL", "provider": "ffz"},
    ]


@respx.mock
async def test_third_party_emotes_partial_failure(respx_mock: respx.MockRouter) -> None:
    _mock_providers(
        respx_mock,
        bttv_global=httpx.Response(503),
        ffz_channel=httpx.Response(404),  # no FFZ room: not a failure
    )
    out = await tpe.fetch_third_party_emotes(TWITCH_ID)
    assert out["failed"] == ["bttv"]
    assert [e["code"] for e in out["bttv"]] == ["catJAM", "pepeD"]  # the part that loaded
    assert [e["code"] for e in out["ffz"]] == ["CatBag"]
    assert len(out["7tv"]) == 3


@respx.mock
async def test_third_party_emotes_route_caches(respx_mock: respx.MockRouter) -> None:
    _mock_providers(respx_mock)
    app = create_app(Settings(twitch_id=TWITCH_ID))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c:
        first = await c.get("/v1/emotes/third-party")
        calls = respx_mock.calls.call_count
        second = await c.get("/v1/emotes/third-party")
    assert first.status_code == 200
    assert set(first.json()) == {"7tv", "bttv", "ffz", "failed"}
    assert second.content == first.content
    assert respx_mock.calls.call_count == calls == 6


# ── Status: live title/category from Helix ────────────────────────────────


class FakeHelix:
    configured = True

    def __init__(self, stream: dict | None, error: Exception | None = None) -> None:
        self.stream = stream
        self.error = error

    async def get_stream(self, user_id: str) -> dict | None:
        if self.error:
            raise self.error
        return self.stream

    async def get_game(self, game_id: str) -> dict | None:
        return {"id": game_id, "box_art_url": f"https://static-cdn.jtvnw.net/ttv-boxart/{game_id}-{{width}}x{{height}}.jpg"}


async def test_helix_stream() -> None:
    live = {"id": "317309253877", "title": "hi", "game_id": "509658", "game_name": "Just Chatting"}
    info = await _helix_stream(FakeHelix(live), TWITCH_ID, "317309253877")
    assert info == {
        "title": "hi",
        "game": {
            "name": "Just Chatting",
            "gameId": "509658",
            "image": "https://static-cdn.jtvnw.net/ttv-boxart/509658-40x53.jpg",
            "imageTemplate": "https://static-cdn.jtvnw.net/ttv-boxart/509658-{width}x{height}.jpg",
        },
    }
    # another stream, offline, or Helix down: fall back to the VOD row
    assert await _helix_stream(FakeHelix(live), TWITCH_ID, "1") is None
    assert await _helix_stream(FakeHelix(None), TWITCH_ID, "317309253877") is None
    assert await _helix_stream(FakeHelix(None, httpx.ConnectError("down")), TWITCH_ID, "317309253877") is None
