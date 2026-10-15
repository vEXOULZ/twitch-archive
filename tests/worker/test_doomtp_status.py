"""The dashboard's doomtp-bot tile: Doomtp.status() over the bot's /log/coverage."""

import httpx
import respx
from archive_worker.doomtp import Doomtp, status_from_coverage
from pydantic import SecretStr

COVERAGE = "https://bot.test/api/v2/channels/vexoulz/log/coverage"


def test_listening_is_an_open_session():
    open_ = {"sessions": [{"started_at": "2026-10-15T10:00:00Z", "ended_at": None}]}
    assert status_from_coverage(open_) == {"listening": True, "since": "2026-10-15T10:00:00Z"}
    ended = {"sessions": [{"started_at": "2026-10-15T10:00:00Z", "ended_at": "2026-10-15T11:00:00Z"}]}
    assert status_from_coverage(ended) == {"listening": False, "since": "2026-10-15T11:00:00Z"}
    assert status_from_coverage({"sessions": []}) == {"listening": False, "since": None}


async def test_not_configured_asks_nothing(settings):
    st = await Doomtp(settings).status()
    assert (st["configured"], st["ok"], st["listening"], st["error"]) == (False, None, None, None)


@respx.mock
async def test_status_reads_coverage_and_is_cached(settings, respx_mock):
    settings.doomtp_url = "https://bot.test/"
    settings.doomtp_api_key = SecretStr("key")
    route = respx_mock.get(COVERAGE).respond(
        json={"sessions": [{"started_at": "2026-10-15T10:00:00Z", "ended_at": None}], "gaps": []}
    )
    bot = Doomtp(settings)
    st = await bot.status()
    assert (st["configured"], st["keyed"], st["login"], st["ok"], st["listening"], st["since"]) == (
        True,
        True,
        "vexoulz",
        True,
        True,
        "2026-10-15T10:00:00Z",
    )
    assert route.calls.last.request.headers["authorization"] == "Bearer key"
    assert "since" in route.calls.last.request.url.params
    await bot.status()
    assert route.call_count == 1  # reused for a minute
    await bot.status(max_age=0)
    assert route.call_count == 2


@respx.mock
async def test_errors_say_why(settings, respx_mock):
    settings.doomtp_url = "https://bot.test"
    respx_mock.get(COVERAGE).respond(403, json={"title": "forbidden"})
    st = await Doomtp(settings).status()
    assert (st["ok"], st["listening"], st["error"]) == (False, None, "HTTP 403: the log isn't public and no key is set")
    respx_mock.get(COVERAGE).mock(side_effect=httpx.ConnectError("down"))
    assert (await Doomtp(settings).status())["error"] == "ConnectError"
