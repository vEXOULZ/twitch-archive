"""Merge and split plans against the site's timeline model (pure functions)."""

import re

import pytest

from archive_worker import timeline
from archive_worker.timeline import GAP_KIND, GAP_NAME, PlanError, Side


# vods-core's model, written out again here so the tests do not grade the module with itself.
def _cuts(vod: Side) -> list[tuple[float, float]]:
    return sorted((c["start"], c["start"] + c["end"]) for c in vod.chapters if c.get("restricted"))


def _parts(vod: Side, typ: str) -> list[float]:
    return [p["duration"] for p in sorted((p for p in vod.youtube if p["type"] == typ), key=lambda p: p["part"])]


def site_delay(vod: Side, typ: str = "vod") -> float:
    """duration − Σ part durations − Σ cut lengths"""
    return vod.duration - sum(_parts(vod, typ)) - sum(e - s for s, e in _cuts(vod))


def site_vod_time(vod: Side, upload_time: float, typ: str = "vod") -> float:
    """Where a frame at ``upload_time`` of the back-to-back parts shows on the VOD (the player skips cuts)."""
    v = upload_time + site_delay(vod, typ)
    for s, e in _cuts(vod):
        if s <= v:
            v += e - s
    return v


def ch(start, length, name="Just Chatting", restricted=False, **extra):
    return {**timeline.chapter("509658", name, None, start, length, restricted), **extra}


def yt(vid, part, duration, typ="vod"):
    return {"id": vid, "type": typ, "duration": duration, "part": part, "thumbnail_url": f"https://t/{vid}"}


def side(vod_id, duration, chapters, youtube, drive=()):
    return Side(vod_id, duration, chapters, youtube, list(drive))


# A: 2 h, 5 s of footage missing at its start. B: starts 7500 s after A (a 300 s gap), 10 s missing.
A = side("a", 7200, [ch(0, 3600), ch(3600, 3600, "Minecraft")], [yt("a1", 1, 3600), yt("a2", 2, 3595)],
         [{"id": "da", "type": "vod"}])
B = side("b", 3600, [ch(0, 1800), ch(1800, 1800, "Minecraft")], [yt("b1", 1, 3590)], [{"id": "db", "type": "vod"}])
OFFSET = 7500


def merged(plan) -> Side:
    return side("a", plan.duration, plan.chapters, plan.youtube)


def test_merge_keeps_both_timelines():
    plan = timeline.plan_merge(A, B, OFFSET)
    m = merged(plan)
    assert plan.duration == OFFSET + B.duration == 11100
    # The formula still gives A's own delay, so A's frames stay where they were ...
    assert site_delay(m) == site_delay(A) == 5
    assert site_vod_time(m, 1000) == site_vod_time(A, 1000)
    # ... and B's first uploaded frame lands at offset + B's delay, as does every later one.
    a_uploads = sum(_parts(A, "vod"))
    assert site_vod_time(m, a_uploads) == OFFSET + site_delay(B) == 7510
    assert site_vod_time(m, a_uploads + 1234.5) == OFFSET + site_vod_time(B, 1234.5)

    gap = [c for c in plan.chapters if c.get("kind") == GAP_KIND]
    assert gap == [{"gameId": None, "name": GAP_NAME, "image": None, "duration": "02:00:00", "start": 7200,
                    "end": 310, "restricted": True, "kind": "gap"}]
    assert [(c["name"], c["start"], c["end"]) for c in plan.chapters] == [
        ("Just Chatting", 0, 3600), ("Minecraft", 3600, 3600), (GAP_NAME, 7200, 310),
        ("Just Chatting", 7510, 1790),  # B's missing start is under the gap chapter
        ("Minecraft", 9300, 1800),
    ]
    assert plan.chapters[3]["duration"] == "02:05:10"
    assert [(p["id"], p["part"]) for p in plan.youtube] == [("a1", 1), ("a2", 2), ("b1", 3)]
    assert plan.drive == [{"id": "da", "type": "vod"}, {"id": "db", "type": "vod"}]
    d = plan.detail
    assert (d["offset"], d["gap"], d["gapChapter"], d["playedType"]) == (7500, 300, {"start": 7200, "end": 7510}, "vod")
    assert d["types"]["vod"] == {"targetDelay": 5, "sourceDelay": 10, "sourceFirstFrame": 10, "mergedDelay": 5,
                                 "drift": 0}
    assert d["clippedChapters"] == 1


def test_merge_gap_covers_cuts_at_the_start_of_the_source():
    b = side("b", 3600, [ch(0, 60, "Artifact", restricted=True), ch(60, 3540)], [yt("b1", 1, 3530)])
    plan = timeline.plan_merge(A, b, OFFSET)
    m = merged(plan)
    assert site_delay(m) == 5
    first = site_vod_time(b, 0)
    assert first == 70  # 10 s delay after the 60 s cut
    assert site_vod_time(m, sum(_parts(A, "vod"))) == OFFSET + first
    assert [(c["name"], c["start"], c["end"]) for c in plan.chapters][2:] == [
        (GAP_NAME, 7200, 370), ("Just Chatting", 7570, 3530)]


def test_merge_with_no_gap_and_no_missing_footage_has_no_gap_chapter():
    a = side("a", 3600, [ch(0, 3600)], [yt("a1", 1, 3600)])
    b = side("b", 3600, [ch(0, 3600)], [yt("b1", 1, 3600)])
    plan = timeline.plan_merge(a, b, 3600)
    assert not any(c.get("kind") for c in plan.chapters)
    assert plan.detail["gapChapter"] is None and site_delay(merged(plan)) == 0


def test_merge_numbers_each_type_on_its_own_and_reports_drift():
    """Both VODs have vod and live uploads; the site plays live, so the gap chapter fits live."""
    a = side("a", 3600, [ch(0, 3600)], [yt("av", 1, 3600), yt("al", 1, 3598, "live")])
    b = side("b", 3600, [ch(0, 3600)], [yt("bv", 1, 3600), yt("bl", 1, 3596, "live")])
    plan = timeline.plan_merge(a, b, 3700)
    assert [(p["id"], p["type"], p["part"]) for p in plan.youtube] == [
        ("av", "vod", 1), ("bv", "vod", 2), ("al", "live", 1), ("bl", "live", 2)]
    m = merged(plan)
    assert plan.detail["playedType"] == "live"
    assert site_delay(m, "live") == site_delay(a, "live") == 2
    assert site_vod_time(m, 3598, "live") == 3700 + site_delay(b, "live")
    # One gap chapter serves both types: the vod uploads' B delay (0 s) differs from live's (4 s).
    assert plan.detail["types"]["vod"]["drift"] == -4


@pytest.mark.parametrize(
    ("a", "b", "offset", "message", "extra"),
    [
        (A, B, 7000, "The VODs overlap", {"offset": 7000, "targetDuration": 7200, "gap": -200}),
        (A, side("b", 3600, [], [yt("b1", 1, 3610)]), 7200, "first uploaded frame of b would land at 7190s", {}),
        (A, side("b", 3600, [], [yt("b1", 1, 3600, "live")]), 7500, "a has vod uploads and b has live uploads", {}),
        (A, side("b", 3600, [], [{"id": "b1", "type": "vod", "part": 1}]), 7500, "vod part 1 (b1) has no duration",
         {}),
    ],
)
def test_merge_refusals(a, b, offset, message, extra):
    with pytest.raises(PlanError, match=re.escape(message)) as exc:
        timeline.plan_merge(a, b, offset)
    assert extra.items() <= exc.value.extra.items()


# ── Split ─────────────────────────────────────────────────────────────────


def test_split_points_are_where_uploads_end():
    assert timeline.split_intervals(A) == [(3605, 3605)]
    with pytest.raises(PlanError, match="inside an upload") as exc:
        timeline.plan_split(A, 3000)
    assert exc.value.extra["validPoints"] == [{"at": 3605, "from": 3605, "to": 3605}]
    with pytest.raises(PlanError, match="inside an upload"):
        timeline.plan_split(A, 3)  # missing footage before the first part: no upload ends there
    # Between two parts, across the cut there, and inside a cut after the last part.
    cut = side("c", 7200, [ch(0, 3000), ch(3000, 600, "Artifact", restricted=True), ch(3600, 3000),
                           ch(6600, 600, "Artifact", restricted=True)], [yt("c1", 1, 3000), yt("c2", 2, 3000)])
    assert timeline.split_intervals(cut) == [(3000, 3600), (6600, 7200)]
    assert timeline.nearest_points(timeline.split_intervals(cut), 4000.4, 7200) == [
        {"at": 3600, "from": 3000, "to": 3600}, {"at": 6600, "from": 6600, "to": 7200}]
    first, second = timeline.plan_split(cut, 3300)
    assert [(c["name"], c["start"], c["end"]) for c in first.chapters][-1] == ("Artifact", 3000, 300)
    assert [(c["name"], c["start"], c["end"]) for c in second.chapters][0] == ("Artifact", 0, 300)
    assert site_delay(merged(first)) == 0 and site_delay(side("c-2", 3900, second.chapters, second.youtube)) == 0
    assert timeline.split_intervals(side("x", 100, [], [])) == [(0, 100)]  # no uploads: anywhere
    # Whole seconds within half a second of a boundary that is not a whole second.
    odd = side("o", 7200, [], [yt("o1", 1, 3599.7), yt("o2", 2, 3600.3)])
    assert timeline.nearest_points(timeline.split_intervals(odd), 10, 7200) == [
        {"at": 3600, "from": 3599.7, "to": 3599.7}]
    timeline.plan_split(odd, 3600)


def test_split_at_a_part_boundary():
    first, second = timeline.plan_split(A, 3605)
    f, s = merged(first), side("a-2", second.duration, second.chapters, second.youtube)
    assert (first.duration, second.duration) == (3605, 3595)
    assert site_delay(f) == 5 and site_delay(s) == 0
    # A frame 395 s into A's part 2 is at VOD time 4000 before, and 395 in the second half.
    assert site_vod_time(A, 3600 + 395) == 4000 and site_vod_time(s, 395) == 4000 - 3605
    assert [(c["name"], c["start"], c["end"]) for c in first.chapters] == [("Just Chatting", 0, 3600),
                                                                         ("Minecraft", 3600, 5)]
    assert [(c["name"], c["start"], c["end"], c["duration"]) for c in second.chapters] == [
        ("Minecraft", 0, 3595, "00:00:00")]
    assert [(p["id"], p["part"]) for p in first.youtube] == [("a1", 1)]
    assert [(p["id"], p["part"]) for p in second.youtube] == [("a2", 1)]
    assert (first.drive, second.drive) == (A.drive, [])


def test_split_inside_a_gap_keeps_a_gap_chapter_on_both_sides():
    plan = timeline.plan_merge(A, B, OFFSET)
    m = side("a", plan.duration, plan.chapters, plan.youtube)
    assert (7200, 7510) in timeline.split_intervals(m)
    first, second = timeline.plan_split(m, 7300)
    assert [(c["name"], c["start"], c["end"], c.get("kind")) for c in first.chapters][-1] == (GAP_NAME, 7200, 100, "gap")
    assert [(c["name"], c["start"], c["end"], c.get("kind")) for c in second.chapters][0] == (GAP_NAME, 0, 210, "gap")
    assert site_delay(merged(first)) == 5
    assert site_delay(side("a-2", second.duration, second.chapters, second.youtube)) == 0
    # At the gap's end the second half has no gap chapter left
    _, second = timeline.plan_split(m, 7510)
    assert not any(c.get("kind") for c in second.chapters)


# ── Emotes ────────────────────────────────────────────────────────────────


def test_union_emotes_without_duplicates():
    a = {"ffz_emotes": [{"id": 1, "code": "x"}], "bttv_emotes": [], "seventv_emotes": [{"id": "s", "code": "EZ"}],
         "global_emotes": None, "global_emotes_source": None, "global_emotes_at": None}
    b = {"ffz_emotes": [{"id": "1", "code": "x"}, {"id": 2, "code": "y"}], "bttv_emotes": [{"id": "b", "code": "z"}],
         "seventv_emotes": [{"id": "s", "code": "EZ"}], "global_emotes": {"7tv": [{"id": "g", "code": "g"}]},
         "global_emotes_source": "captured", "global_emotes_at": "2026-01-01T00:00:00+00:00"}
    out = timeline.union_emotes(a, b)
    assert out["ffz_emotes"] == [{"id": 1, "code": "x"}, {"id": 2, "code": "y"}]
    assert out["bttv_emotes"] == [{"id": "b", "code": "z"}]
    assert out["seventv_emotes"] == [{"id": "s", "code": "EZ"}]
    assert out["global_emotes"] == {"7tv": [{"id": "g", "code": "g"}]}
    assert out["global_emotes_source"] == "captured"
    assert timeline.union_emotes(None, b) is b and timeline.union_emotes(a, None) is a
