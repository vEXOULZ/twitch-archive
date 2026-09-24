from archive_worker import hls

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60 (source)",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,CODECS="avc1.64002A,mp4a.40.2",VIDEO="chunked",FRAME-RATE=60.000
https://d1m7jfoe9zdc1j.cloudfront.net/abc_vexoulz_1/chunked/index-dvr.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="720p60",NAME="720p60",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,VIDEO="720p60"
https://d1m7jfoe9zdc1j.cloudfront.net/abc_vexoulz_1/720p60/index-dvr.m3u8
"""

MASTER_NO_CHUNKED = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,VIDEO="1080p60"
https://host.example/abc_vexoulz_1/1080p60/index-dvr.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,VIDEO="720p60"
https://host.example/abc_vexoulz_1/720p60/index-dvr.m3u8
"""


def test_parse_master():
    variants = hls.parse_master(MASTER)
    assert [v.video for v in variants] == ["chunked", "720p60"]
    assert variants[0].height == 1080
    assert variants[0].name == "1080p60 (source)"
    assert variants[0].bandwidth == 8000000


def test_candidates_prefer_chunked():
    urls = hls.twitch_variant_candidates(hls.parse_master(MASTER))
    assert urls == ["https://d1m7jfoe9zdc1j.cloudfront.net/abc_vexoulz_1/chunked/index-dvr.m3u8"]


def test_candidates_derive_chunked_then_1080p():
    urls = hls.twitch_variant_candidates(hls.parse_master(MASTER_NO_CHUNKED))
    assert urls == [
        "https://host.example/abc_vexoulz_1/chunked/index-dvr.m3u8",
        "https://host.example/abc_vexoulz_1/1080p60/index-dvr.m3u8",
    ]


def test_usher_urls():
    url = hls.vod_master_url("123", '{"a":1}', "sig")
    assert url.startswith("https://usher.ttvnw.net/vod/v2/123.m3u8?")
    assert "platform=web" in url and "transcode_mode=cbr_v1" in url and "nauthsig=sig" in url
    assert "/api/channel/hls/vexoulz.m3u8?" in hls.live_master_url("VexOulz", "t", "s")


VOD_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#ID3-EQUIV-TDTG:2026-02-21T03:00:00
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-TWITCH-ELAPSED-SECS:0.000
#EXT-X-TWITCH-TOTAL-SECS:25.500
#EXTINF:10.000,
0.ts
#EXTINF:10.000,
1-muted.ts
#EXTINF:5.500,
2.ts
#EXT-X-ENDLIST
"""


def test_parse_vod_playlist():
    pl = hls.parse_media(VOD_PLAYLIST)
    assert [s.uri for s in pl.segments] == ["0.ts", "1-muted.ts", "2.ts"]
    assert [s.sequence for s in pl.segments] == [0, 1, 2]
    assert pl.total_seconds == 25.5
    assert pl.ended
    assert pl.init_uri is None


FMP4_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:2
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:2.000,
0.mp4
"""


def test_parse_init_map():
    pl = hls.parse_media(FMP4_PLAYLIST)
    assert pl.init_uri == "init-0.mp4"
    assert not pl.ended


LIVE_WITH_ADS = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:500
#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="2026-02-21T03:00:04.000Z",DURATION=4.0
#EXT-X-PROGRAM-DATE-TIME:2026-02-21T03:00:00.000Z
#EXTINF:2.000,live
https://video-edge/seg500.ts
#EXT-X-PROGRAM-DATE-TIME:2026-02-21T03:00:02.000Z
#EXTINF:2.000,live
https://video-edge/seg501.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-02-21T03:00:04.000Z
#EXTINF:2.000,
https://video-edge/ad1.ts
#EXT-X-PROGRAM-DATE-TIME:2026-02-21T03:00:06.000Z
#EXTINF:2.000,Amazon|123
https://video-edge/ad2.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-02-21T03:00:08.000Z
#EXTINF:2.000,live
https://video-edge/seg504.ts
"""


def test_live_ad_detection_and_sequence():
    pl = hls.parse_media(LIVE_WITH_ADS)
    assert [s.sequence for s in pl.segments] == [500, 501, 502, 503, 504]
    assert [s.ad for s in pl.segments] == [False, False, True, True, False]
    assert pl.segments[2].discontinuity and pl.segments[4].discontinuity


def test_segment_names():
    assert hls.unmuted_name("12-muted.ts") == "12.ts"
    assert hls.unmuted_name("12-unmuted.ts") == "12.ts"
    assert hls.unmuted_name("12.ts") == "12.ts"
    assert hls.local_name("https://h/x/y/5.ts?token=1") == "5.ts"
    assert hls.local_name("5-muted.ts") == "5-muted.ts"


def test_write_local_playlist():
    text = hls.write_local_playlist([("a.ts", 10.0, False), ("b.ts", 12.4, True)], init_name="init.mp4")
    assert text.startswith("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:13\n")
    assert '#EXT-X-MAP:URI="init.mp4"' in text
    assert "#EXT-X-DISCONTINUITY\n#EXTINF:12.400,\nb.ts" in text
    assert text.endswith("#EXT-X-ENDLIST\n")
