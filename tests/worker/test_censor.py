"""Censor ranges: validation, the plan the censor step runs and the move onto a live recording (pure functions)."""

from typing import Any

import pytest
from archive_worker import planning, vod_edits


def test_censor_ranges_are_checked_and_sorted():
    out = vod_edits.censor(
        [
            {"start": 100, "end": 160.5, "mute": True},
            {"start": 10, "end": 20, "video": "blur", "note": "  in-game nudity "},
            {"start": 10, "end": 15, "video": "black", "mute": False},
        ],
        7200,
    )
    assert out == [
        {"start": 10, "end": 15, "video": "black", "mute": False},
        {"start": 10, "end": 20, "video": "blur", "mute": False, "note": "in-game nudity"},
        {"start": 100, "end": 160.5, "video": None, "mute": True},
    ]
    assert vod_edits.censor([], 7200) == []


@pytest.mark.parametrize(
    ("item", "error"),
    [
        ({"start": -1, "end": 5, "mute": True}, r"start must be"),
        ({"start": 5, "end": 5, "mute": True}, r"end must be"),
        ({"start": 5, "end": 7300, "mute": True}, r"after the end of the VOD"),
        ({"start": 5, "end": 6, "video": "pixelate"}, r"video must be"),
        ({"start": 5, "end": 6, "mute": "yes"}, r"mute must be"),
        ({"start": 5, "end": 6}, r"neither covers"),
        ({"start": 5, "end": 6, "mute": True, "note": "x" * 201}, r"longer than"),
        ({"start": 5, "end": 6, "mute": True, "colour": "red"}, r"colour"),
    ],
)
def test_censor_refuses_bad_ranges(item, error):
    with pytest.raises(ValueError, match=error):
        vod_edits.censor([item], 7200)


def test_censor_length_is_unchecked_without_a_duration():
    assert vod_edits.censor([{"start": 0, "end": 99999, "mute": True}], 0)[0]["end"] == 99999


def test_plan_censor_black_wins_and_same_modes_join():
    plan = planning.plan_censor(
        [
            {"start": 0, "end": 30, "video": "blur", "mute": False},
            {"start": 10, "end": 20, "video": "black", "mute": True},
            {"start": 30, "end": 40, "video": "blur", "mute": False},  # touches the first: one blur
            {"start": 15, "end": 25, "video": None, "mute": True},
            {"start": 100, "end": 110, "video": "black", "mute": False},
        ]
    )
    assert plan.cover == [(0, 10, "blur"), (10, 20, "black"), (20, 40, "blur"), (100, 110, "black")]
    assert plan.mute == [(10, 25)]
    assert not plan.empty
    assert planning.plan_censor([]).empty
    assert planning.plan_censor(None).empty


def test_censor_on_recording_follows_the_timeline():
    start = 1_000_000.0
    # Recorded 0-100 s of the VOD, then a 50 s ad break, then from 150 s on.
    timeline = [[0, start, 100], [100, start + 150, 1000]]
    ranges: list[dict[str, Any]] = [
        {"start": 10, "end": 20, "video": "blur", "mute": False},
        {"start": 110, "end": 140, "video": "black", "mute": False},  # inside the break: not recorded
        {"start": 90, "end": 160, "video": None, "mute": True},  # across it
    ]
    moved = planning.censor_on_recording(ranges, start, timeline)
    assert [(r["start"], r["end"]) for r in moved] == [(10, 20), (90, 110)]
    assert moved[1]["mute"] is True
