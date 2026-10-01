"""Synthetic VODs: segments, their validation, and the columns derived from their sources (pure)."""

import datetime as dt

import pytest

from archive_common.segments import Segment, resolve, total
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


def test_no_synthetic_of_synthetics():
    nested = Source("a+b", 100.0, [], START, synthetic=True)
    with pytest.raises(ComposeError, match="synthetic"):
        compose.validate([Segment("a+b", 0, None, 0)], {"a+b": nested})


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
