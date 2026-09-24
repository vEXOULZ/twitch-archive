import datetime as dt

from archive_worker import planning
from archive_worker.planning import Part


def _edge(pos_ms, dur_ms, name, gid="1"):
    return {
        "node": {
            "positionMilliseconds": pos_ms,
            "durationMilliseconds": dur_ms,
            "details": {"game": {"id": gid, "displayName": name, "boxArtURL": f"https://img/{gid}.jpg"}},
        }
    }


def test_chapters_keep_legacy_shape():
    chapters = planning.chapters_from_moments(
        [_edge(0, 3_600_000, "Just Chatting"), _edge(3_600_000, 0, "Artifact", "2")], 5000.5, ["Artifact"]
    )
    assert chapters[0] == {
        "gameId": "1", "name": "Just Chatting", "image": "https://img/1.jpg",
        "duration": "00:00:00", "start": 0, "end": 3600, "restricted": False,
    }
    # durationMilliseconds 0 => runs to the end of the VOD; ``end`` is a length
    assert chapters[1]["duration"] == "01:00:00"
    assert chapters[1]["end"] == 1400.5
    assert chapters[1]["restricted"] is True


def test_single_chapter_box_art():
    ch = planning.single_chapter({"id": "9", "displayName": "Chess"}, "https://b/{width}x{height}.jpg", 100, [])
    assert ch["image"] == "https://b/40x53.jpg"
    assert ch["start"] == 0 and ch["end"] == 100 and ch["duration"] == "00:00:00"
    assert planning.single_chapter(None, None, 100, [])["name"] is None


def test_plan_parts_no_chapters():
    parts = planning.plan_parts(25_000, [], [], 10_800)
    assert parts == [Part(1, 0, 10_800), Part(2, 10_800, 21_600), Part(3, 21_600, 25_000)]


def test_plan_parts_skip_restricted():
    chapters = [
        {"name": "A", "start": 0, "end": 1000},
        {"name": "Artifact", "start": 1000, "end": 500},
        {"name": "B", "start": 1500, "end": 12_000},
    ]
    parts = planning.plan_parts(13_500, chapters, ["Artifact"], 10_800)
    assert parts == [Part(1, 0, 1000), Part(2, 1500, 12_300), Part(3, 12_300, 13_500)]


def test_plan_parts_flag_and_whole_vod_restricted():
    chapters = [{"name": "X", "start": 0, "end": 100, "restricted": True}]
    assert planning.plan_parts(100, chapters, [], 10_800) == []


def test_select_parts():
    parts = planning.plan_parts(40_000, None, [], 10_800)
    assert [p.number for p in planning.select_parts(parts, 2, 3)] == [2, 3]
    assert [p.number for p in planning.select_parts(parts, None, None)] == [1, 2, 3, 4]
    assert [p.number for p in planning.select_parts(parts, 4, None)] == [4]


def test_titles_use_local_date():
    created = dt.datetime(2026, 2, 21, 1, 30, tzinfo=dt.timezone.utc)  # 20th in Sao Paulo
    assert planning.video_title("vexoulz", "vod", created, "America/Sao_Paulo", 1, 1) == (
        "vexoulz Twitch VOD - 2026-02-20"
    )
    assert planning.video_title("vexoulz", "live", created, "America/Sao_Paulo", 2, 3) == (
        "vexoulz Twitch Live VOD - 2026-02-20 PART 2"
    )


def test_descriptions_are_rebuilt():
    base = planning.base_description("vods.example.net", "100", "Hi <b>there</b>", "VOD")
    assert base == "Chat Replay: https://vods.example.net/youtube/100\nStream Title: Hi bthere/b\nVOD"
    chapters = [
        {"name": "A", "start": 0, "end": 11_000},
        {"name": "Artifact", "start": 11_000, "end": 100},
        {"name": "B", "start": 11_100, "end": 500},
    ]
    part2 = Part(2, 10_800, 21_600)
    lines = planning.chapter_lines(chapters, part2, ["Artifact"])
    assert lines == ["00:00:00 A", "00:05:00 B"]
    siblings = [{"id": "y2", "part": 2}, {"id": "y1", "part": 1}]
    text = planning.full_description(base, 2, siblings, lines)
    assert text.startswith("PART 1: https://youtube.com/watch?v=y1\n\nChat Replay:")
    assert text.endswith("\n\n00:00:00 A\n00:05:00 B")
    assert planning.full_description(base, 2, siblings, lines) == text


def test_privacy():
    assert planning.privacy("vod", public=False, multi_track=False) == "unlisted"
    assert planning.privacy("vod", public=True, multi_track=False) == "public"
    assert planning.privacy("vod", public=True, multi_track=True) == "unlisted"
    assert planning.privacy("live", public=True, multi_track=True) == "public"


def test_upsert_youtube_entry():
    entries = [{"id": "a", "type": "vod", "part": 1}, {"id": "b", "type": "vod", "part": 2}]
    out = planning.upsert_youtube_entry(entries, {"id": "c", "type": "vod", "part": 2})
    assert [e["id"] for e in out] == ["a", "c"]
    out = planning.upsert_youtube_entry(out, {"id": "l", "type": "live", "part": 1})
    assert [e["id"] for e in out] == ["a", "c", "l"]


def _claim(kind, start, length, policy="POLICY_TYPE_GLOBAL_BLOCK"):
    return {
        "type": kind,
        "claimPolicy": {"primaryPolicy": {"policyType": policy}},
        "matchDetails": {"longestMatchStartTimeSeconds": str(start), "longestMatchDurationSeconds": str(length)},
    }


def test_plan_dmca():
    plan = planning.plan_dmca(
        [
            _claim("CLAIM_TYPE_AUDIO", 10, 20),
            _claim("CLAIM_TYPE_AUDIO", 25, 10),
            _claim("CLAIM_TYPE_VISUAL", 100, 5),
            _claim("CLAIM_TYPE_AUDIOVISUAL", 200, 10),
            _claim("CLAIM_TYPE_AUDIO", 300, 10, policy="POLICY_TYPE_MONETIZE"),
        ]
    )
    assert plan.mute == [(10, 35), (200, 210)]
    assert plan.blackout == [(100, 105), (200, 210)]
    assert planning.plan_dmca([]).empty
