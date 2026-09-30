"""doomtp-bot chat into bot_logs: the conversions, the client, the steps (dev DB) and the
comments API's choice of source (dev DB)."""

import base64
import datetime as dt
import json
import uuid

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import delete, select, update

from archive_common.config import Settings
from archive_common.db import get_sessionmaker
from archive_common.models import BotLog, Job, Log, Stream, Vod, VodSplice
from archive_worker.context import StepError, StepRefused
from archive_worker.doomtp import Doomtp
from archive_worker.monitor import Monitor
from archive_worker.steps import bot_chat as step
from archive_worker.steps.metadata import DEFAULT_COLOR
from archive_worker.vods import resequence_bot_logs

BOT = "https://bot.test"
LOG_URL = f"{BOT}/api/v1/channels/vexoulz/log"
START = dt.datetime(2001, 3, 4, 20, 0, tzinfo=dt.timezone.utc)
START_MS = int(START.timestamp() * 1000)
VOD, VOD2 = "test-bot-chat", "test-bot-chat-2"


def _message(mid, at_s, text="hi", user="u1", **extra):
    return {"kind": "message", "id": mid, "at": START_MS + int(at_s * 1000),
            "user": {"id": user, "login": f"login{user}", "display_name": f"User{user}"},
            "text": text, "fragments": [{"type": "text", "text": text}],
            "badges": [{"set_id": "subscriber", "id": "12", "info": "14"}], "color": "#FF0000", **extra}


# ── Conversions ───────────────────────────────────────────────────────────


def test_fragments_in_the_replay_shape():
    frags = [
        {"type": "text", "text": "héllo "},
        {"type": "emote", "text": "vexoulHELP", "emote_id": "emotesv2_abc", "emote": {"format": ["static"]}},
        {"type": "text", "text": " "},
        {"type": "mention", "text": "@someone", "mention": {"id": "9", "login": "someone"}},
    ]
    assert step.replay_fragments(frags) == [
        {"text": "héllo ", "emote": None, "__typename": "VideoCommentMessageFragment"},
        {"text": "vexoulHELP", "emote": {"id": "emotesv2_abc;6;15", "from": 6, "emoteID": "emotesv2_abc",
                                         "__typename": "EmbeddedEmote"},
         "__typename": "VideoCommentMessageFragment"},
        {"text": " ", "emote": None, "__typename": "VideoCommentMessageFragment"},
        {"text": "@someone", "emote": None, "__typename": "VideoCommentMessageFragment"},
    ]
    assert step.replay_fragments(None, "plain") == [
        {"text": "plain", "emote": None, "__typename": "VideoCommentMessageFragment"}]


def test_badges_in_the_replay_shape():
    got = step.replay_badges([{"set_id": "subscriber", "id": "3012", "info": "14"}, {"set_id": "", "id": "1"}])
    assert got == [{"id": base64.b64encode(b"subscriber;3012;").decode(), "setID": "subscriber", "version": "3012",
                    "__typename": "Badge"}]


def test_notice_text_and_offsets():
    assert step.notice_text({"system_message": "A gifted a sub!", "text": "enjoy"}, "sub_gift") == "A gifted a sub! enjoy"
    assert step.notice_text({"system_message": "", "text": None}, "raid") == "raid"
    assert step.offset_seconds(START_MS + 1999, START) == 1
    assert step.offset_seconds(START_MS - 5000, START) == 0


def test_rows_for_each_kind():
    looks: dict = {}
    msg = step.to_row(_message("m1", 3.5, reward_id="r1", deleted_at=START_MS + 9000), VOD, START, looks)
    assert (msg["kind"], msg["content_offset_seconds"], msg["user_color"], msg["display_name"]) == (
        "message", 3, "#FF0000", "Useru1")
    assert msg["deleted_at"] == START + dt.timedelta(seconds=9) and msg["data"]["reward_id"] == "r1"
    assert msg["user_badges"][0]["setID"] == "subscriber"

    notice = step.to_row({"kind": "notification", "id": "n1", "at": START_MS + 7000, "type": "resub",
                          "user": {"id": "u1", "login": "loginu1", "display_name": "Useru1"},
                          "payload": {"system_message": "Useru1 subscribed for 3 months!", "text": "yo"}},
                         VOD, START, looks)
    assert notice["kind"] == "notice" and notice["message"][0]["text"] == "Useru1 subscribed for 3 months! yo"
    assert (notice["user_badges"], notice["user_color"]) == (msg["user_badges"], "#FF0000")  # from their message
    stranger = step.to_row({"kind": "notification", "id": "n2", "at": START_MS, "type": "raid", "user": {"id": "x"},
                            "payload": {}}, VOD, START, looks)
    assert (stranger["user_badges"], stranger["user_color"]) == ([], DEFAULT_COLOR)

    mod = step.to_row({"kind": "moderation", "id": 17, "at": START_MS, "type": "timeout",
                       "target": {"id": "u1", "login": "loginu1"}}, VOD, START, looks)
    assert (mod["id"], mod["kind"], mod["user_id"], mod["message"]) == ("mod:17", "moderation", "u1", [])
    assert step.to_row({"kind": "other", "id": "z", "at": 1}, VOD, START, looks) is None


# ── Client ────────────────────────────────────────────────────────────────


@respx.mock
async def test_client_pages_and_sends_the_key_only_when_set(settings):
    settings.doomtp_url = BOT + "/"
    pages = {None: {"entries": [{"id": 1}, {"id": 2}], "next": "c2"}, "c2": {"entries": [{"id": 3}], "next": None}}
    route = respx.get(LOG_URL).mock(side_effect=lambda r: httpx.Response(200, json=pages[r.url.params.get("cursor")]))
    client = Doomtp(settings)
    assert client.configured and not client.keyed
    assert [e["id"] async for e in client.log(1, 2)] == [1, 2, 3]
    first = route.calls[0].request
    assert (first.url.params["since"], first.url.params["until"], first.url.params["order"]) == ("1", "2", "asc")
    assert "authorization" not in first.headers and "python" not in first.headers["user-agent"].lower()

    settings.doomtp_api_key = SecretStr("read-key")
    settings.doomtp_login = "other"
    respx.get(f"{BOT}/api/v1/channels/other/log").mock(return_value=httpx.Response(200, json={"entries": []}))
    assert [e async for e in Doomtp(settings).log(1, 2)] == []
    assert respx.calls.last.request.headers["authorization"] == "Bearer read-key"
    assert not Doomtp(Settings(doomtp_url="")).configured


# ── Steps (dev DB) ────────────────────────────────────────────────────────


async def _clean():
    async with get_sessionmaker()() as s:
        await s.execute(delete(Job).where(Job.vod_id.in_((VOD, VOD2))))
        await s.execute(delete(VodSplice).where(VodSplice.vod_id.in_((VOD, VOD2))))
        for model in (BotLog, Log):
            await s.execute(delete(model).where(model.vod_id.in_((VOD, VOD2))))
        await s.execute(delete(Vod).where(Vod.id.in_((VOD, VOD2))))
        await s.commit()


@pytest.fixture
async def vod(db, settings):
    settings.doomtp_url = BOT
    await _clean()
    async with get_sessionmaker()() as s:
        s.add_all([Vod(id=VOD, title="t", created_at=START, duration="01:00:00"),
                   Vod(id=VOD2, title="t2", created_at=START - dt.timedelta(days=1), duration="00:10:00")])
        await s.commit()
    yield VOD
    await _clean()


def _serve(entries):
    """A /log answering from ``entries`` by since/until, 2 per page."""

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        rows = [e for e in entries if int(p["since"]) <= e["at"] < int(p["until"])]
        start = int(p.get("cursor") or 0)
        nxt = str(start + 2) if start + 2 < len(rows) else None
        return httpx.Response(200, json={"entries": rows[start : start + 2], "next": nxt})

    return handler


async def _rows(vod_id=VOD):
    async with get_sessionmaker()() as s:
        return list((await s.execute(select(BotLog).where(BotLog.vod_id == vod_id)
                                     .order_by(BotLog.content_offset_seconds, BotLog.seq))).scalars())


@respx.mock
async def test_one_shot_stores_merges_and_annotates(vod, make_ctx):
    entries = [
        _message("m1", 1, "first", reward_id="r1"),
        {"kind": "notification", "id": "n1", "at": START_MS + 2000, "type": "redemption", "user": {"id": "u1"},
         "payload": {"system_message": "Useru1 redeemed Hydrate", "reward": {"id": "r1", "title": "Hydrate",
                                                                            "cost": 500}, "input": "first"}},
        _message("m2", 10, "rude", user="u2"),
        {"kind": "moderation", "id": 5, "at": START_MS + 12000, "type": "timeout", "target": {"id": "u2"},
         "moderator": {"login": "mod"}, "duration_s": 600},
        _message("late", 4000, "after the vod"),  # past the 1 h VOD
    ]
    respx.get(f"{LOG_URL}/coverage").mock(return_value=httpx.Response(200, json={"gaps": []}))
    respx.get(LOG_URL).mock(side_effect=_serve(entries))
    await step.bot_chat(make_ctx("bot_chat", VOD))

    rows = {r.id: r for r in await _rows()}
    assert set(rows) == {"m1", "n1", "m2", "mod:5"}
    assert rows["m1"].data["reward"] == {"id": "r1", "title": "Hydrate", "cost": 500, "input": "first"}
    assert rows["m2"].cleared_at == START + dt.timedelta(seconds=12)
    assert rows["m2"].data["removal"]["type"] == "timeout"
    assert rows["m1"].cleared_at is None
    async with get_sessionmaker()() as s:
        info = (await s.get(Vod, VOD)).bot_chat
    assert (info["rows"], info["keyed"], info["coverage"]) == (4, False, {"gaps": []})

    # Read again later: the bot has since seen m1 deleted; nothing is duplicated, our keys stay.
    entries[0] = _message("m1", 1, "first", reward_id="r1", deleted_at=START_MS + 60_000)
    await step.bot_chat(make_ctx("bot_chat", VOD))
    rows = {r.id: r for r in await _rows()}
    assert len(rows) == 4 and rows["m1"].deleted_at == START + dt.timedelta(minutes=1)
    assert rows["m1"].data["reward"]["title"] == "Hydrate"


@respx.mock
async def test_late_rows_are_resequenced(vod, make_ctx):
    respx.get(f"{LOG_URL}/coverage").mock(return_value=httpx.Response(200, json={}))
    entries = [_message("b", 20), _message("c", 30)]
    respx.get(LOG_URL).mock(side_effect=_serve(entries))
    await step.bot_chat(make_ctx("bot_chat", VOD))
    entries.insert(0, _message("a", 5))  # the bot filled a gap: an earlier message, stored last
    await step.bot_chat(make_ctx("bot_chat", VOD))
    rows = await _rows()
    assert [r.id for r in rows] == ["a", "b", "c"]
    assert [r.seq for r in rows] == sorted(r.seq for r in rows)
    async with get_sessionmaker()() as s:
        assert await resequence_bot_logs(s, VOD) == []  # already in order: nothing renumbered


async def test_refusals_and_off(vod, make_ctx, settings):
    async with get_sessionmaker()() as s:
        (await s.get(Vod, VOD2)).merged_into = {"id": VOD, "offset": 100}
        await s.commit()
    with pytest.raises(StepRefused):
        await step.bot_chat(make_ctx("bot_chat", VOD2))
    settings.doomtp_url = ""
    await step.bot_chat(make_ctx("bot_chat", VOD))  # off: nothing fetched (respx is not active)
    with pytest.raises(StepError):
        await step.bot_chat_backfill(make_ctx("bot_chat_backfill", None))


@respx.mock
async def test_backfill_skips_done_and_spliced_vods(vod, make_ctx):
    respx.get(f"{LOG_URL}/coverage").mock(return_value=httpx.Response(200, json={}))
    route = respx.get(LOG_URL).mock(return_value=httpx.Response(200, json={"entries": []}))
    async with get_sessionmaker()() as s:
        (await s.get(Vod, VOD2)).merged_into = {"id": VOD, "offset": 100}
        await s.commit()
    await step.bot_chat_backfill(make_ctx("bot_chat_backfill", None, {"vod_ids": [VOD, VOD2]}))
    assert [dict(c.request.url.params)["since"] for c in route.calls] == [str(START_MS)]  # only VOD
    async with get_sessionmaker()() as s:
        assert (await s.get(Vod, VOD)).bot_chat is not None and (await s.get(Vod, VOD2)).bot_chat is None


# ── Comments API (dev DB) ─────────────────────────────────────────────────


async def _add(bot: int, replay: int):
    async with get_sessionmaker()() as s:
        s.add_all([Log(id=uuid.uuid4(), vod_id=VOD, display_name="r", content_offset_seconds=i,
                       message=[{"text": f"replay {i}"}], user_badges=[], user_color="#fff",
                       created_at=START + dt.timedelta(seconds=i)) for i in range(replay)])
        s.add_all([BotLog(id=f"b{i}", vod_id=VOD, kind="message", at=START + dt.timedelta(seconds=i),
                          content_offset_seconds=i, message=[{"text": f"bot {i}"}], user_badges=[], user_color="#fff",
                          data={"text": f"bot {i}"}) for i in range(bot)])
        s.add(BotLog(id="mod:1", vod_id=VOD, kind="moderation", at=START, content_offset_seconds=0, message=[],
                     user_badges=[], user_color="#fff", data={}))
        await s.commit()


async def test_api_prefers_the_bot_unless_it_is_partial(vod, api):
    await _add(bot=5, replay=10)  # a partial bot log: the replay
    async with api:
        page = (await api.get(f"/v1/vods/{VOD}/comments?content_offset_seconds=0")).json()
        assert {c["source"] for c in page["comments"]} == {"replay"} and len(page["comments"]) == 10
        page = (await api.get(f"/v1/vods/{VOD}/comments?content_offset_seconds=0&source=bot")).json()
        assert [c["id"] for c in page["comments"]] == [f"b{i}" for i in range(5)]  # no moderation rows
        first = page["comments"][0]
        assert (first["source"], first["kind"], first["bot"], first["createdAt"]) == (
            "bot", "message", {"text": "bot 0"}, "2001-03-04T20:00:00.000Z")
        assert (await api.get(f"/v1/vods/{VOD}/comments?content_offset_seconds=0&source=x")).status_code == 400


async def test_api_pages_bot_chat_by_cursor(vod, api):
    await _add(bot=450, replay=400)
    async with api:
        seen, page = [], (await api.get(f"/v1/vods/{VOD}/comments?content_offset_seconds=0")).json()
        while True:
            seen += [c["id"] for c in page["comments"]]
            assert {c["source"] for c in page["comments"]} == {"bot"}
            if "cursor" not in page:
                break
            assert json.loads(base64.b64decode(page["cursor"]))["src"] == "bot"
            # A cursor keeps its source, whatever ?source says.
            page = (await api.get(f"/v1/vods/{VOD}/comments?cursor={page['cursor']}&source=replay")).json()
        assert seen == [f"b{i}" for i in range(450)]

        page = (await api.get(f"/v1/vods/{VOD}/comments?content_offset_seconds=0&source=replay")).json()
        assert "src" not in json.loads(base64.b64decode(page["cursor"]))
        page = (await api.get(f"/v1/vods/{VOD}/comments?content_offset_seconds=300")).json()
        assert "bot 300" in [c["message"][0]["text"] for c in page["comments"]]


async def test_monitor_starts_bot_chat_when_the_stream_ends(vod, settings):
    stream_id = "990000000001"

    class FakeHelix:
        def __init__(self):
            self.settings, self.live = settings, True

        async def get_stream(self, _user_id):
            return {"id": stream_id, "started_at": START.isoformat()} if self.live else None

        async def video_for_stream(self, _user_id, sid):
            return {"id": VOD, "stream_id": sid, "title": "t", "created_at": START.isoformat(), "duration": "30m0s"}

    class FakeRunner:
        def __init__(self):
            self.enqueued = []

        async def enqueue(self, kind, vod_id, payload):
            self.enqueued.append((kind, vod_id, payload))

    settings.live_record = settings.vod_download = False
    helix, runner = FakeHelix(), FakeRunner()
    monitor = Monitor(helix, runner)
    live = select(Stream.id).where(Stream.is_live.is_(True))
    async with get_sessionmaker()() as s:  # the dev DB's own live stream is not this test's
        was_live = list((await s.execute(live)).scalars())
        await s.execute(update(Stream).where(Stream.id.in_(was_live)).values(is_live=False))
        await s.commit()
    try:
        await monitor.tick()
        assert runner.enqueued == []  # live: the bot is recording it
        helix.live = False
        await monitor.tick()
        assert runner.enqueued == [("bot_chat", VOD, {"stream_id": stream_id, "duration": 1800})]
        await monitor.tick()  # already marked offline: not again
        assert len(runner.enqueued) == 1
    finally:
        async with get_sessionmaker()() as s:
            await s.execute(delete(Stream).where(Stream.id == int(stream_id)))
            await s.execute(update(Stream).where(Stream.id.in_(was_live)).values(is_live=True))
            await s.commit()


@respx.mock
async def test_payload_duration_bounds_the_read(vod, make_ctx):
    respx.get(f"{LOG_URL}/coverage").mock(return_value=httpx.Response(200, json={}))
    respx.get(LOG_URL).mock(side_effect=_serve([_message("in", 100), _message("out", 2000)]))
    await step.bot_chat(make_ctx("bot_chat", VOD, {"duration": 1800}))  # the row still says 1 h
    assert [r.id for r in await _rows()] == ["in"]
