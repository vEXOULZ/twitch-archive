import json

import httpx
import pytest
import respx

from archive_common.twitch.gql import GQL_URL
from archive_worker import hls
from archive_worker.steps import capture as cap


def _token_response(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    key = "streamPlaybackAccessToken" if body["variables"]["isLive"] else "videoPlaybackAccessToken"
    return httpx.Response(200, json={"data": {key: {"value": '{"t":1}', "signature": "abc"}}})


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,VIDEO="1080p60"
https://cdn.example/h_vexoulz_1/1080p60/index-dvr.m3u8
"""


@respx.mock
async def test_variant_falls_back_to_1080p_on_403(make_ctx):
    respx.post(GQL_URL).mock(side_effect=_token_response)
    respx.get(url__startswith="https://usher.ttvnw.net/vod/v2/100.m3u8").respond(200, text=MASTER)
    chunked = respx.get("https://cdn.example/h_vexoulz_1/chunked/index-dvr.m3u8").respond(403)
    respx.get("https://cdn.example/h_vexoulz_1/1080p60/index-dvr.m3u8").respond(200, text="#EXTM3U")
    url = await cap._resolve_variant(make_ctx())
    assert chunked.called
    assert url == "https://cdn.example/h_vexoulz_1/1080p60/index-dvr.m3u8"


@respx.mock
async def test_variant_other_errors_propagate(make_ctx):
    respx.post(GQL_URL).mock(side_effect=_token_response)
    respx.get(url__startswith="https://usher.ttvnw.net/vod/v2/100.m3u8").respond(200, text=MASTER)
    respx.get("https://cdn.example/h_vexoulz_1/chunked/index-dvr.m3u8").respond(404)
    with pytest.raises(httpx.HTTPStatusError):
        await cap._resolve_variant(make_ctx())


PLAYLIST = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10.000,
0.ts
#EXTINF:10.000,
1-muted.ts
#EXTINF:4.000,
2-muted.ts
"""


@respx.mock
async def test_sync_prefers_unmuted_copies(make_ctx):
    ctx = make_ctx()
    ctx.hls_dir.mkdir(parents=True)
    (ctx.hls_dir / "1.ts").write_bytes(b"unmuted-original")  # captured before Twitch muted it
    base = "https://cdn.example/h/chunked"
    r0 = respx.get(f"{base}/0.ts").respond(200, content=b"zero")
    r1 = respx.get(f"{base}/1-muted.ts").respond(200, content=b"muted")
    r2 = respx.get(f"{base}/2-muted.ts").respond(200, content=b"muted2")

    missing, failed = await cap._sync_segments(ctx, hls.parse_media(PLAYLIST), base)

    assert (missing, failed) == (2, 0)
    assert r0.called and r2.called and not r1.called
    local = (ctx.hls_dir / "index.m3u8").read_text()
    assert "\n1.ts\n" in local and "1-muted.ts" not in local and "\n2-muted.ts\n" in local
    assert (ctx.hls_dir / "1.ts").read_bytes() == b"unmuted-original"
    # a second pass downloads nothing new
    assert await cap._sync_segments(ctx, hls.parse_media(PLAYLIST), base) == (0, 0)


LIVE_1 = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:10
#EXTINF:2.000,live
https://edge.example/s10.ts
#EXTINF:2.000,live
https://edge.example/s11.ts
#EXT-X-DISCONTINUITY
#EXTINF:2.000,Amazon|1
https://edge.example/ad.ts
"""

LIVE_2 = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:11
#EXTINF:2.000,live
https://edge.example/s11.ts
#EXTINF:2.000,Amazon|1
https://edge.example/ad.ts
#EXT-X-DISCONTINUITY
#EXTINF:2.000,live
https://edge.example/s13.ts
#EXT-X-ENDLIST
"""


@respx.mock
async def test_live_record_skips_ads_and_handles_rollover(make_ctx, settings, monkeypatch):
    settings.live_poll_interval_seconds = 0
    ctx = make_ctx("live", None, {"type": "live", "stream_id": "555", "login": "vexoulz"})

    async def no_save():
        return None

    monkeypatch.setattr(ctx, "save", no_save)
    respx.post(GQL_URL).mock(side_effect=_token_response)
    respx.get(url__startswith="https://usher.ttvnw.net/api/channel/hls/vexoulz.m3u8").respond(
        200, text='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,VIDEO="chunked"\nhttps://edge.example/live.m3u8\n'
    )
    respx.get("https://edge.example/live.m3u8").mock(
        side_effect=[httpx.Response(200, text=LIVE_1), httpx.Response(200, text=LIVE_2)]
    )
    for n in (10, 11, 13):
        respx.get(f"https://edge.example/s{n}.ts").respond(200, content=f"seg{n}".encode())
    ad = respx.get("https://edge.example/ad.ts").respond(200, content=b"ad")

    await cap.live_record(ctx)

    assert not ad.called
    assert ctx.payload["last_seq"] == 13
    text = (ctx.hls_dir / "index.m3u8").read_text()
    names = [line for line in text.splitlines() if line.endswith(".ts")]
    assert names == ["000000010.ts", "000000011.ts", "000000013.ts"]
    # the ad break becomes a discontinuity before the next real segment
    assert "#EXT-X-DISCONTINUITY\n#EXTINF:2.000,\n000000013.ts" in text
