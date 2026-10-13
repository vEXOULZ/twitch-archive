"""Whether the uploads are still on YouTube and whole (youtube_check.py)."""

from typing import Any

from archive_worker import youtube_check as yc


def video(seconds: str = "PT2H", upload: str = "processed", **more: Any) -> dict[str, Any]:
    details = {"duration": seconds, **more.pop("details", {})}
    return {"status": {"uploadStatus": upload, **more}, "contentDetails": details}


def entry(vid: str, part: int = 1, duration: float = 7200, kind: str = "vod", **more: Any) -> dict[str, Any]:
    return {"id": vid, "type": kind, "part": part, "duration": duration, **more}


def kinds(issues: list[dict[str, Any]]) -> list[str]:
    return [i["kind"] for i in issues]


def test_iso_seconds():
    assert yc.iso_seconds("PT1H2M3S") == 3723
    assert yc.iso_seconds("PT45S") == 45
    assert yc.iso_seconds("P1DT1M") == 86460
    assert yc.iso_seconds("PT0S") == 0
    assert yc.iso_seconds("1:00:00") is None
    assert yc.iso_seconds(None) is None


def test_a_whole_processed_upload_has_no_issues():
    assert yc.video_issues(entry("a"), video()) == []
    assert yc.video_issues(entry("a"), video("PT1H59M57S")) == []  # within SHORT_BY


def test_removed_rejected_and_failed_are_bad():
    assert yc.video_issues(entry("a"), None)[0]["kind"] == "removed"
    assert kinds(yc.video_issues(entry("a"), video(upload="deleted"))) == ["removed"]
    [rejected] = yc.video_issues(entry("a"), video(upload="rejected", rejectionReason="copyright"))
    assert (rejected["kind"], rejected["severity"]) == ("rejected", yc.BAD)
    assert "copyright" in rejected["detail"]
    [failed] = yc.video_issues(entry("a"), video(upload="failed", failureReason="codec"))
    assert failed["severity"] == yc.BAD and "codec" in failed["detail"]


def test_warnings():
    assert kinds(yc.video_issues(entry("a"), video(upload="uploaded"))) == ["processing"]
    blocked = video(details={"regionRestriction": {"blocked": ["DE", "FR"]}})
    assert [i["detail"] for i in yc.video_issues(entry("a"), blocked)] == ["Blocked in 2 countries"]
    only = video(details={"regionRestriction": {"allowed": ["US"]}})
    assert [i["detail"] for i in yc.video_issues(entry("a"), only)] == ["Only viewable in 1 country"]
    aged = video(details={"contentRating": {"ytRating": "ytAgeRestricted"}})
    assert kinds(yc.video_issues(entry("a"), aged)) == ["ageRestricted"]
    [cut] = yc.video_issues(entry("a"), video("PT1H30M"))
    assert (cut["kind"], cut["severity"]) == ("shorter", yc.WARN)
    assert "01:30:00" in cut["detail"]


def test_report_counts_the_best_copy_and_skips_bad_parts():
    entries = [
        entry("v1", 1, 3600),
        entry("v2", 2, 3600),
        entry("l1", 1, 7000, kind="live", gaps=[[100, 300]]),
    ]
    videos = {"v1": video("PT1H"), "l1": video("PT1H56M40S")}  # v2 is gone
    r = yc.vod_report("100", "t", 7200, 7200, entries, videos)
    vod, live = r["copies"]
    assert (vod["type"], vod["parts"], vod["seconds"]) == ("vod", 2, 3600)
    assert (live["type"], live["seconds"], live["gaps"]) == ("live", 7000, 200)
    assert r["share"] == live["share"] == 7000 / 7200
    assert kinds(r["issues"]) == ["removed"]  # live plus its ad breaks covers the plan
    assert r["issues"][0] | {"type": "vod", "part": 2, "video": "v2"} == r["issues"][0]


def test_report_flags_missing_parts_and_incomplete_copies():
    r = yc.vod_report(
        "100",
        None,
        10800,
        10800,
        [entry("v1", 1, 3600), entry("v3", 3, 3600)],
        {
            "v1": video("PT1H"),
            "v3": video("PT1H"),
        },
    )
    assert kinds(r["issues"]) == ["missingPart", "incomplete"]
    assert r["issues"][0]["part"] == 2
    assert "01:00:00" in r["issues"][1]["detail"]
    # Planned less than the VOD (a restricted game left out): a copy that holds the plan is whole.
    whole = yc.vod_report("100", None, 10800, 3600, [entry("v1", 1, 3600)], {"v1": video("PT1H")})
    assert whole["issues"] == [] and whole["share"] == 1 / 3
