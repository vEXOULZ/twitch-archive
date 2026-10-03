"""Chat from doomtp-bot's log API into ``bot_logs``, next to the replay crawl's ``logs``.

The bot records chat live on its own, so like the replay's ``chat`` step this reads a
VOD once, after its stream ends: the monitor starts a ``bot_chat`` job then, separate
from the ``archive`` job. Each row keeps the bot's entry in ``data`` and gets
``message``/``user_badges``/``user_color`` in the replay's shape, so the comments API can
serve either table to the same frontends. Only ever adds or updates ``bot_logs`` rows.
"""

from __future__ import annotations

import base64
import datetime as dt
import math
import re
from typing import Any

import httpx
from archive_common.db import get_sessionmaker
from archive_common.models import BotLog, Vod
from sqlalchemy import func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from ..context import JobContext, StepError
from ..events import iso_utc
from ..vods import notify_rows_moved, resequence_bot_logs, splice_reason
from .metadata import DEFAULT_COLOR, vod_duration

REDEMPTION_MATCH_S = 10  # a redeem's message and its redemption notice, this close together
# _upsert inlines every value of every row as a bind parameter, and one statement takes at most
# 32767 of them. (The chat step's executemany has no such limit, so CHAT_BATCH is too big here.)
BOT_BATCH = 32767 // len(BotLog.__table__.columns)


def _ms(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)


def _from_ms(ms: int | float | None) -> dt.datetime | None:
    return None if ms is None else dt.datetime.fromtimestamp(ms / 1000, dt.UTC)


# ── Bot entry -> replay shape ─────────────────────────────────────────────


# A GIF from Twitch's GIF picker is a GIPHY one: only GIPHY's own CDN is linked, as with emotes.
_GIPHY_ID = re.compile(r"[A-Za-z0-9]{1,64}")
_GIPHY_URL = re.compile(
    r"https://(?:media[0-9]?|i)\.giphy\.com/media/(?:v1\.[A-Za-z0-9_=.-]+/)?([A-Za-z0-9]{1,64})/"
    r"[A-Za-z0-9_.-]+\.(?:gif|webp)"
)


def replay_gif(frag: dict[str, Any]) -> dict[str, Any] | None:
    """A ``gif`` fragment's ``{id, url, still, title}``: the GIPHY id, the animated GIF, its still
    first frame, and the text Twitch shows for it without the brackets. None without a GIPHY id."""
    gif = frag.get("gif") if isinstance(frag.get("gif"), dict) else {}
    url = gif.get("url") if isinstance(gif.get("url"), str) else ""  # type: ignore[union-attr]
    linked = _GIPHY_URL.fullmatch(url)  # type: ignore[arg-type]
    gif_id = gif.get("id") if isinstance(gif.get("id"), str) and _GIPHY_ID.fullmatch(gif["id"]) else None  # type: ignore[index, union-attr]
    gif_id = gif_id or (linked[1] if linked else None)
    if gif_id is None:
        return None
    media = f"https://media.giphy.com/media/{gif_id}"
    title = (frag.get("text") or "").strip()
    if title.startswith("[") and title.endswith("]"):
        title = title[1:-1].strip()
    return {
        "id": gif_id,
        "url": url if linked and linked[1] == gif_id else f"{media}/giphy.gif",
        "still": f"{media}/giphy_s.gif",
        "title": title or None,
    }


def replay_fragments(fragments: list[dict[str, Any]] | None, fallback_text: str = "") -> list[dict[str, Any]]:
    """Bot fragments as the replay stores them: text, or text plus an embedded emote; a GIF adds
    ``gif`` (``replay_gif``), which the replay never has. Mentions and cheermotes are plain text there
    (their details stay in ``data``)."""
    out: list[dict[str, Any]] = []
    pos = 0
    for frag in fragments or [{"type": "text", "text": fallback_text}]:
        frag_text = frag.get("text") or ""
        emote = None
        if frag.get("type") == "emote" and frag.get("emote_id"):
            emote_id = frag["emote_id"]
            emote = {
                "id": f"{emote_id};{pos};{pos + len(frag_text) - 1}",
                "from": pos,
                "emoteID": emote_id,
                "__typename": "EmbeddedEmote",
            }
        gif = replay_gif(frag) if frag.get("type") == "gif" else None
        out.append(
            {
                "text": frag_text,
                "emote": emote,
                **({"gif": gif} if gif else {}),
                "__typename": "VideoCommentMessageFragment",
            }
        )
        pos += len(frag_text)
    return out


def replay_badges(badges: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for b in badges or []:
        set_id, version = str(b.get("set_id") or ""), str(b.get("id") or "")
        if not set_id:
            continue
        badge_id = base64.b64encode(f"{set_id};{version};".encode()).decode("ascii")
        out.append({"id": badge_id, "setID": set_id, "version": version, "__typename": "Badge"})
    return out


def notice_text(payload: dict[str, Any] | None, kind: str) -> str:
    payload = payload or {}
    system, extra = payload.get("system_message") or "", payload.get("text") or ""
    return " ".join(p for p in (system or kind, extra) if p)


def offset_seconds(at_ms: int, vod_start: dt.datetime) -> int:
    return max(0, math.floor((at_ms - _ms(vod_start)) / 1000))


def to_row(
    entry: dict[str, Any], vod_id: str, vod_start: dt.datetime, looks: dict[str, tuple[list[Any], str]]
) -> dict[str, Any] | None:
    """A ``bot_logs`` row for one bot entry, or None for a kind this does not keep.

    ``looks`` maps a user id to the badges and color of their latest message, for
    notices (the bot has neither on them); messages update it.
    """
    kind = entry.get("kind")
    at_ms = entry.get("at")
    if kind not in ("message", "notification", "moderation") or at_ms is None or entry.get("id") is None:
        return None
    user = entry.get("user") or {}
    row: dict[str, Any] = {
        "id": str(entry["id"]),
        "vod_id": vod_id,
        "at": _from_ms(at_ms),
        "content_offset_seconds": offset_seconds(at_ms, vod_start),
        "user_id": user.get("id"),
        "user_login": user.get("login"),
        "display_name": user.get("display_name") or user.get("login"),
        "message_type": None,
        "deleted_at": None,
        "cleared_at": None,
        "data": entry,
    }
    if kind == "message":
        badges, color = replay_badges(entry.get("badges")), entry.get("color") or DEFAULT_COLOR
        if user.get("id"):
            looks[user["id"]] = (badges, color)
        row.update(
            kind="message",
            message=replay_fragments(entry.get("fragments"), entry.get("text") or ""),
            user_badges=badges,
            user_color=color,
            message_type=entry.get("message_type"),
            deleted_at=_from_ms(entry.get("deleted_at")),
            cleared_at=_from_ms(entry.get("cleared_at")),
        )
    elif kind == "notification":
        badges, color = looks.get(user.get("id") or "", ([], DEFAULT_COLOR))
        row.update(
            kind="notice",
            user_badges=badges,
            user_color=color,
            message=replay_fragments(None, notice_text(entry.get("payload"), entry.get("type") or "")),
        )
    else:
        target = entry.get("target") or {}
        row.update(
            id=f"mod:{entry['id']}",
            kind="moderation",
            message=[],
            user_badges=[],
            user_color=DEFAULT_COLOR,
            user_id=target.get("id"),
            user_login=target.get("login"),
            display_name=target.get("display_name") or target.get("login"),
        )
    return row


# ── Storing ───────────────────────────────────────────────────────────────


def annotate(rows: list[dict[str, Any]]) -> None:
    """Fold notices and moderation into the messages they concern (rows in time order).

    A redeem's message has only the reward id; its redemption notice has the title and
    cost. A moderation action (only keyed reads have these) flags what it removed.
    """
    messages = [r for r in rows if r["kind"] == "message"]
    by_id = {r["id"]: r for r in messages}
    by_user: dict[str, list[dict[str, Any]]] = {}
    for r in messages:
        by_user.setdefault(r["user_id"] or "", []).append(r)
    slack = dt.timedelta(seconds=REDEMPTION_MATCH_S)
    for r in rows:
        e = r["data"]
        if r["kind"] == "notice" and e.get("type") == "redemption":
            payload = e.get("payload") or {}
            reward = payload.get("reward") or {}
            user_id = (payload.get("user") or e.get("user") or {}).get("id")
            info = {
                k: v
                for k, v in {**reward, "input": payload.get("input"), "status": payload.get("status")}.items()
                if v is not None
            }
            for m in by_user.get(user_id or "", []) if reward.get("id") else []:
                if m["data"].get("reward_id") == reward["id"] and abs(m["at"] - r["at"]) <= slack:
                    m["data"] = {**m["data"], "reward": info}
        elif r["kind"] == "moderation":
            typ, at = e.get("type"), r["at"]
            if typ == "delete":
                targets = [by_id[mid]] if (mid := str(e.get("message_id"))) in by_id else []
            elif typ == "chat_clear":
                targets = [m for m in messages if m["at"] <= at and m["cleared_at"] is None]
            elif typ in ("timeout", "ban", "user_clear") and r["user_id"]:
                targets = [m for m in by_user.get(str(r["user_id"]), []) if m["at"] <= at and m["cleared_at"] is None]
            else:
                continue
            removal = {k: e[k] for k in ("type", "moderator", "reason", "duration_s", "at") if e.get(k) is not None}
            for m in targets:
                if typ == "delete":
                    m["deleted_at"] = m["deleted_at"] or at
                else:
                    m["cleared_at"] = at
                m["data"] = {**m["data"], "removal": removal}


def _upsert(rows: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    stmt = insert(BotLog).values(rows)
    ex = stmt.excluded
    # A later read adds what the bot has learned since (a message deleted since, a filled-in
    # field); nothing is cleared. Unchanged rows are left alone.
    values = {
        "deleted_at": func.coalesce(ex.deleted_at, BotLog.deleted_at),
        "cleared_at": func.coalesce(ex.cleared_at, BotLog.cleared_at),
        "message_type": func.coalesce(ex.message_type, BotLog.message_type),
        "data": BotLog.data.op("||")(ex.data),
    }
    current = {
        "deleted_at": BotLog.deleted_at,
        "cleared_at": BotLog.cleared_at,
        "message_type": BotLog.message_type,
        "data": BotLog.data,
    }
    changed = or_(*(current[k].is_distinct_from(v) for k, v in values.items()))
    return stmt.on_conflict_do_update(
        index_elements=[BotLog.id], set_={**values, "updatedAt": func.now()}, where=changed
    ).returning(literal_column("xmax = 0"))  # true: inserted


async def read_vod(ctx: JobContext, vod: Vod) -> None:
    """Store the bot's entries for the whole VOD, [start, start + duration), and record the read."""
    doomtp = ctx.deps.doomtp
    duration = await vod_duration(ctx, vod)
    since = vod.created_at
    until = since + dt.timedelta(seconds=duration) if duration > 0 else dt.datetime.now(dt.UTC)
    looks: dict[str, tuple[list[Any], str]] = {}
    rows = [
        row
        async for entry in doomtp.log(_ms(since), _ms(until))
        if (row := to_row(entry, vod.id, vod.created_at, looks)) is not None
    ]
    annotate(rows)

    info: dict[str, Any] = {
        "fetched_at": iso_utc(dt.datetime.now(dt.UTC)),
        "since": since.isoformat(),
        "until": until.isoformat(),
        "keyed": doomtp.keyed,
    }
    try:
        info["coverage"] = await doomtp.coverage(_ms(since), _ms(until))
    except httpx.HTTPError as exc:
        ctx.log.warning("could not read the bot's coverage: %s", exc)
    for gap in (info.get("coverage") or {}).get("gaps") or []:
        ctx.log.warning(
            "bot log gap %s -> %s (%s)", _from_ms(gap.get("from")), _from_ms(gap.get("to")), gap.get("reason")
        )

    inserted = changed = 0
    async with get_sessionmaker()() as s:
        for i in range(0, len(rows), BOT_BATCH):
            written = (await s.execute(_upsert(rows[i : i + BOT_BATCH]))).scalars().all()
            changed += len(written)
            inserted += sum(written)
        if changed:
            await resequence_bot_logs(s, vod.id)
            await notify_rows_moved(s, vod.id)
        info["rows"] = (
            await s.execute(select(func.count()).select_from(BotLog).where(BotLog.vod_id == vod.id))
        ).scalar_one()
        await s.execute(update(Vod).where(Vod.id == vod.id).values(bot_chat=info))
        await s.commit()
    ctx.log.info(
        "bot chat for %s: %d entries read, %d new, %d updated", vod.id, len(rows), inserted, changed - inserted
    )


async def bot_chat(ctx: JobContext) -> None:
    """Read the VOD's chat from doomtp-bot (``payload.duration``: the stream's final length)."""
    if not ctx.deps.doomtp.configured:
        ctx.log.info("doomtp-bot chat disabled (ARCHIVE_DOOMTP_URL unset)")
        return
    await ctx.refuse_if_spliced()  # offsets are from this VOD's start; a spliced VOD has moved them
    await read_vod(ctx, await ctx.get_vod())


async def bot_chat_backfill(ctx: JobContext) -> None:
    """Bot chat for VODs that never had it (``payload.vod_ids``: only those), newest first.

    Queues one ``bot_chat`` run per VOD, its children (``payload.backfill``): each retries and
    fails on its own, and they share one lock (``jobs._lock``), so they run one at a time beside
    the live jobs. Only adds ``bot_logs`` rows; the replay chat is not touched. Merged or split
    VODs, and VODs with a ``bot_chat`` run already queued or running, are skipped.
    """
    if not ctx.deps.doomtp.configured:
        raise StepError("ARCHIVE_DOOMTP_URL is not set")
    from .. import jobs  # jobs imports the steps

    stmt = select(Vod).where(Vod.merged_into.is_(None)).order_by(Vod.created_at.desc())
    if ctx.payload.get("vod_ids"):
        stmt = stmt.where(Vod.id.in_([str(v) for v in ctx.payload["vod_ids"]]))
    else:
        stmt = stmt.where(Vod.bot_chat.is_(None))
    async with get_sessionmaker()() as s:
        vods = list((await s.execute(stmt)).scalars())
    queued = 0
    for i, vod in enumerate(vods):
        ctx.progress(100 * i / len(vods), 100, "percent", f"bot chat backfill: {vod.id} ({i + 1}/{len(vods)})")
        reason = await splice_reason(vod.id)
        if reason:
            ctx.log.info("skipping %s: %s", vod.id, reason)
            continue
        if await jobs.find_active("bot_chat", vod_id=vod.id):
            ctx.log.info("skipping %s: a bot_chat job is already queued or running", vod.id)
            continue
        job_id = await ctx.enqueue("bot_chat", vod.id, {"backfill": True})
        ctx.log.info("queued bot chat for %s: job %d", vod.id, job_id)
        queued += 1
    ctx.progress(100, 100, "percent", "bot chat backfill queued")
    ctx.log.info("bot chat backfill: %d job(s) queued for %d VOD(s)", queued, len(vods))
