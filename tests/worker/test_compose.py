"""Synthetic VODs: segments, their validation, and the columns derived from their sources (pure)."""

import datetime as dt

import pytest

from archive_common.segments import Segment, flatten, resolve, total
from archive_worker import compose
from archive_worker.compose import ComposeError, Source

START = dt.datetime(2001, 2, 3, 20, 0, tzinfo=dt.timezone.utc)


def _ch(start, length, name="Just Chatting", game_id="509658", restricted=False):
    return {"gameId": game_id, "name": name, "image": None, "duration": "00:00:00", "start": start, "end": length,
            "restricted": restricted}


A = Source("a", 7200.0, [_ch(0, 3600), _ch(3600, 3600, "Minecraft", "27471")], START, "https://t/a", "big stream")
B = Source("b", 3600.0, [_ch(0, 3600, "Minecraft", "27471")], START + dt.timedelta(seconds=7500), "https://t/b", "b")
SOURCES = {"a": A, "b": B}


def test_parse_defaults_and_back_to_back():
    segs = compose.parse([{"vodId": "a", "start": 10, "end": 100}, {"vodId": "b", "end": 50, "label": " two "},
                          {"vodId": "a", "start": 200, "at": 500}])
    assert segs == [Segment("a", 10, 100, 0), Segment("b", 0, 50, 90, "two"), Segment("a", 200, None, 500)]


@pytest.mark.parametrize("items, message", [
    ([], "non-empty"),
    ([{"vodId": "a", "start": 5, "end": 5}], "end after it starts"),
    ([{"vodId": "a"}, {"vodId": "b"}], "no 'end' to put it after"),
    ([{"vodId": "a", "start": -1}], ">= 0"),
    ([{"vodId": "a", "nope": 1}], "unknown field"),
    ([{"vodId": ""}], "vodId"),
    ([{"vodId": "a", "label": "x" * 201}], "label"),
])
def test_parse_refused(items, message):
    with pytest.raises(ComposeError, match=message):
        compose.parse(items)


@pytest.mark.parametrize("vod_id", ["123", "", "-a", "A", "a b", "x" * 101])
def test_check_id_refused(vod_id):
    with pytest.raises(ComposeError):
        compose.check_id(vod_id)


def test_check_id():
    for vod_id in ("123-1", "123+456", "p-minecraft", "a_b"):
        assert compose.check_id(vod_id) == vod_id


@pytest.mark.parametrize("segments, message", [
    ([Segment("a", 0, 10, 5)], "must be at 0"),
    ([Segment("x", 0, 10, 0)], "no VOD x"),
    ([Segment("a", 7200, None, 0)], "at or after the end"),
    ([Segment("a", 0, 7300, 0)], "after the end"),
    ([Segment("a", 0, 100, 0), Segment("b", 0, None, 50)], "inside segments"),
    ([Segment("a", 0, 100, 0), Segment("b", 0, 10, 200), Segment("b", 20, 30, 150)], "sort segments"),
])
def test_validate_refused(segments, message):
    with pytest.raises(ComposeError, match=message):
        compose.validate(segments, SOURCES)


def test_synthetic_sources_validate_like_real_ones():
    merged = Source("a+b", 100.0, [], START, synthetic=True)
    compose.validate([Segment("a+b", 0, None, 0)], {"a+b": merged})
    with pytest.raises(ComposeError, match=r"after the end of a\+b"):
        compose.validate([Segment("a+b", 0, 200, 0)], {"a+b": merged})


def test_nesting_refuses_cycles_and_depth():
    inner = {"m": [Segment("a", 0, None, 0)], "p": [Segment("m", 0, 10, 0)]}
    compose.check_nesting("q", [Segment("p", 0, None, 0)], inner)
    with pytest.raises(ComposeError, match="m -> p -> m: a synthetic VOD cannot be made of itself"):
        compose.check_nesting("m", [Segment("p", 0, None, 0)], inner)
    deep = {"s1": [Segment("s2", 0, None, 0)], "s2": [Segment("s3", 0, None, 0)], "s3": [Segment("s4", 0, None, 0)],
            "s4": [Segment("a", 0, None, 0)]}
    compose.check_nesting("top", [Segment("s2", 0, None, 0)], deep)  # s2, s3, s4: 3 levels
    with pytest.raises(ComposeError, match="nested at most 3 deep"):
        compose.check_nesting("top", [Segment("s1", 0, None, 0)], deep)


def test_flatten_maps_windows_of_synthetic_sources_to_real_vods():
    # m: a merge of a (0-100) and b (from 120); p plays m 50-150, then c, then m again 200-250.
    inner = {"m": [Segment("a", 0, 100, 0), Segment("b", 0, 300, 120)]}
    top = [Segment("m", 50, 150, 0, "first"), Segment("c", 10, 20, 100), Segment("m", 200, 250, 110)]
    assert flatten(top, inner, {"m": True}) == [
        Segment("a", 50, 100, 0, "first", 0),
        Segment("b", 0, 30, 70, "first", 0),  # the gap (100-120 of m) plays nothing
        Segment("c", 10, 20, 100, None, 1),
        Segment("b", 80, 130, 110, None, 2),  # m again after c: a new run, so a new stream
    ]
    # A playthrough inside a playthrough keeps its streams apart; the same window shows the same way.
    assert [x.stream for x in flatten(top, inner, {"m": False})] == [0, 1, 2, 3]
    assert [x.stream for x in flatten([Segment("a", 0, 10, 0), Segment("a", 50, 60, 10)], {}, {})] == [0, 0]
    # A merge of its own is one broadcast: one stream.
    assert [x.stream for x in flatten(top, inner, {"m": True}, one_stream=True)] == [0, 0, 0, 0]


def test_flatten_goes_down_and_stops_at_the_depth_limit():
    inner = {"x": [Segment("y", 0, 10, 0)], "y": [Segment("a", 100, 110, 0)]}
    assert flatten([Segment("x", 2, 5, 0)], inner, {}) == [Segment("a", 102, 105, 0, None, 0)]
    loop = {"x": [Segment("x", 0, 10, 0)]}
    with pytest.raises(ValueError, match="nested more than"):
        flatten([Segment("x", 0, 10, 0)], loop, {})


def test_resolve_open_end_follows_the_source_and_stops_at_the_next():
    segs = [Segment("a", 7000, None, 0), Segment("b", 0, None, 100)]
    assert resolve(segs, {"a": 7200, "b": 3600}) == [Segment("a", 7000, 7100, 0), Segment("b", 0, 3600, 100)]
    # The source grew: an open end follows it.
    assert resolve(segs[1:], {"b": 4000})[0].end == 4000
    assert total(resolve(segs, {"a": 7200, "b": 3600})) == 3700


def test_derive_merge_with_gap():
    segs = compose.merge_segments(A, B)
    assert segs == [Segment("a", 0, None, 0), Segment("b", 0, None, 7500)]
    compose.validate(segs, SOURCES)
    d = compose.derive(segs, SOURCES)
    assert d["duration"] == "03:05:00"
    assert d["created_at"] == START and d["thumbnail_url"] == "https://t/a"
    starts = [(c["start"], c["end"], c.get("kind")) for c in d["chapters"]]
    assert starts == [(0, 3600, None), (3600, 3600, None), (7200, 300, "gap"), (7500, 3600, None)]
    assert d["chapters"][2]["restricted"] is True


def test_merge_overlap_cuts_the_first():
    b = Source("b", 3600.0, [], START + dt.timedelta(seconds=7000))
    segs = compose.merge_segments(A, b)
    assert segs == [Segment("a", 0, 7000, 0), Segment("b", 0, None, 7000)]
    assert compose.derive(segs, {"a": A, "b": b})["duration"] == "02:56:40"


def test_merge_with_explicit_gap_and_refusals():
    assert compose.merge_segments(A, B, 10)[1].at == 7210
    with pytest.raises(ComposeError, match="itself"):
        compose.merge_segments(A, A)
    with pytest.raises(ComposeError, match="other way round"):
        compose.merge_segments(B, A)
    with pytest.raises(ComposeError, match="gap"):
        compose.merge_segments(A, B, -1)


def test_split_anywhere():
    first, second = compose.split_segments(A, 1234.5)
    assert first == [Segment("a", 0, 1234.5, 0)] and second == [Segment("a", 1234.5, None, 0)]
    d1, d2 = compose.derive(first, SOURCES), compose.derive(second, SOURCES)
    assert d1["duration"] == "00:20:35" and d2["duration"] == "01:39:26"
    assert [(c["start"], c["end"]) for c in d1["chapters"]] == [(0, 1234.5)]
    assert [(c["start"], c["end"]) for c in d2["chapters"]] == [(0, 3600 - 1234.5), (3600 - 1234.5, 3600)]
    assert d2["created_at"] == START + dt.timedelta(seconds=1234.5)
    for at in (0, 7200, "x", -5):
        with pytest.raises(ComposeError):
            compose.split_segments(A, at)
    assert compose.split_ids("123") == ("123-1", "123-2") and compose.merge_id("1", "2") == "1+2"


def test_playthrough_windows_back_to_back():
    chapters = [_ch(0, 600), _ch(600, 1200, "Minecraft", "27471"), _ch(1800, 0.5, "Minecraft", "27471"),
                _ch(1800.5, 100, "Minecraft", "27471"), _ch(2000, 300, "Minecraft", "27471", restricted=True),
                _ch(2400, 600, "Minecraft", "27471")]
    assert compose.game_windows(chapters, "27471") == [(600, 1900.5), (2400, 3000)]
    windows = [{"vodId": "a", "start": 3600, "end": 7200}, {"vodId": "b", "start": 0, "end": 3600}]
    segs = compose.parse(windows)
    compose.validate(segs, SOURCES)
    d = compose.derive(segs, SOURCES)
    assert d["duration"] == "02:00:00"
    assert [(c["start"], c["end"], c["name"]) for c in d["chapters"]] == [(0, 3600, "Minecraft"),
                                                                         (3600, 3600, "Minecraft")]
