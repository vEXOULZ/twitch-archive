"""HLS capture: follow a Twitch VOD playlist (``capture``) or the live stream
itself (``live_record``), downloading segments into ``<work>/hls/``."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx

from archive_common import http
from archive_common.timeutil import format_hhmmss

from .. import hls
from ..context import JobContext, StepError

SEGMENT_RETRY_STATUSES = (403, 429, 500, 502, 503, 504)  # Twitch lists segments before they are ready


def _abs(base: str, uri: str) -> str:
    return uri if "://" in uri else f"{base}/{uri}"


async def _download(url: str, dest: Path) -> None:
    resp = await http.request("GET", url, attempts=4, retry_statuses=SEGMENT_RETRY_STATUSES, max_wait=8)
    tmp = dest.with_name(dest.name + ".tmp")

    def write() -> None:
        tmp.write_bytes(resp.content)
        os.replace(tmp, dest)

    await asyncio.to_thread(write)


async def _download_all(items: list[tuple[str, Path]], concurrency: int) -> list[tuple[Path, BaseException]]:
    sem = asyncio.Semaphore(concurrency)
    failures: list[tuple[Path, BaseException]] = []

    async def one(url: str, dest: Path) -> None:
        async with sem:
            try:
                await _download(url, dest)
            except (httpx.HTTPError, OSError) as exc:
                failures.append((dest, exc))

    await asyncio.gather(*(one(u, d) for u, d in items))
    return failures


# ── VOD playlist ──────────────────────────────────────────────────────────


async def _resolve_variant(ctx: JobContext) -> tuple[str, str]:
    """The first available variant: (url, its media playlist)."""
    vod_id = ctx.require_vod_id()
    tok = await ctx.deps.gql.vod_access_token(vod_id)
    master = await http.request("GET", hls.vod_master_url(vod_id, tok.value, tok.signature))
    variants = hls.parse_master(master.text)
    if not variants:
        raise StepError("no variants in VOD master playlist")
    last: BaseException | None = None
    for url in hls.twitch_variant_candidates(variants):
        try:
            return url, (await http.request("GET", url, attempts=2)).text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 403:
                raise
            ctx.log.warning("variant %s unavailable (403), trying next", url)
            last = exc
    raise StepError(f"no available variant for VOD {ctx.vod_id}: {last}")


async def _fetch_vod_playlist(ctx: JobContext) -> tuple[hls.MediaPlaylist, str]:
    url = ctx.payload.get("variant_url")
    if url:
        try:
            resp = await http.request("GET", url, attempts=2)
            return hls.parse_media(resp.text), hls.base_url(url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (403, 404):
                raise
            ctx.log.info("stored variant returned %s, re-resolving", exc.response.status_code)
    url, text = await _resolve_variant(ctx)
    ctx.payload["variant_url"] = url
    await ctx.save()
    return hls.parse_media(text), hls.base_url(url)


async def _sync_segments(ctx: JobContext, pl: hls.MediaPlaylist, base: str) -> tuple[int, int]:
    """Download missing segments and rewrite the local playlist. Returns (missing, failed)."""
    d = ctx.hls_dir
    d.mkdir(parents=True, exist_ok=True)
    have = set(await asyncio.to_thread(os.listdir, d))  # one listing instead of a stat per segment
    todo: list[tuple[str, Path]] = []
    init_name = None
    if pl.init_uri:
        init_name = hls.local_name(pl.init_uri)
        if init_name not in have:
            todo.append((_abs(base, pl.init_uri), d / init_name))

    entries: list[tuple[str, float, bool]] = []
    for seg in pl.segments:
        name = hls.local_name(seg.uri)
        plain = hls.unmuted_name(name)
        if plain in have:
            name = plain  # captured before Twitch muted it
        elif name not in have:
            todo.append((_abs(base, seg.uri), d / name))
        entries.append((name, seg.duration, False))

    failures = await _download_all(todo, ctx.settings.segment_concurrency) if todo else []
    for dest, exc in failures[:5]:
        ctx.log.warning("segment %s failed: %s", dest.name, exc)
    failed = {dest.name for dest, _ in failures}
    have.update(dest.name for _, dest in todo if dest.name not in failed)

    present = [e for e in entries if e[0] in have]
    playlist = hls.write_local_playlist(present, init_name=init_name, target_duration=pl.target_duration)
    (d / "index.m3u8").write_text(playlist, encoding="utf-8")
    ctx.payload["fmp4"] = bool(init_name)
    return len(todo), len(failures)


async def capture(ctx: JobContext, *, one_shot: bool = False) -> None:
    """Follow the VOD playlist until the stream is over (or once, for past VODs)."""
    s = ctx.settings
    helix = ctx.deps.helix
    vod_id = ctx.require_vod_id()
    # ensure_source runs this inside its own step; the flag stops a retry of that
    # step from capturing again.
    if ctx.payload.get("capture_done"):
        return
    last_sig = None
    no_change = 0
    errors = 0
    while True:
        video, fetched = await asyncio.gather(
            helix.get_video(vod_id) if helix.configured else asyncio.sleep(0, {"id": vod_id}),
            _fetch_vod_playlist(ctx),
            return_exceptions=True,
        )
        if isinstance(video, BaseException):
            raise video
        if isinstance(fetched, BaseException):  # token/usher/playlist failures
            if not isinstance(fetched, Exception):
                raise fetched
            errors += 1
            captured = (ctx.hls_dir / "index.m3u8").exists()
            if video is None and captured:
                ctx.log.info("VOD %s is gone from Twitch; finishing with what was captured", vod_id)
                break
            if video is None or one_shot or errors >= 30:
                raise StepError(f"could not fetch VOD playlist for {vod_id}: {fetched}") from fetched
            ctx.log.warning("playlist fetch failed (%s), retrying", fetched)
            await asyncio.sleep(s.hls_poll_interval_seconds)
            continue
        pl, base = fetched
        errors = 0

        if pl.total_seconds:
            await ctx.update_vod(duration=format_hhmmss(pl.total_seconds))
        missing, failed = await _sync_segments(ctx, pl, base)
        ctx.log.info("capture %s: %d segments, %d new, %d failed", vod_id, len(pl.segments), missing, failed)
        have = len(pl.segments) - failed  # the playlist grows while the stream is live
        ctx.progress(have, len(pl.segments), "parts", f"captured {have}/{len(pl.segments)} segments")

        if one_shot:
            if failed:
                raise StepError(f"{failed} segment(s) failed to download")
            break
        if video is None:
            ctx.log.info("VOD %s no longer on Twitch; capture finished", vod_id)
            break
        if pl.ended and not failed:
            if not await _still_live(ctx, str(video.get("stream_id"))):
                ctx.log.info("VOD playlist ended and the stream is offline; capture finished")
                break
        sig = (len(pl.segments), pl.segments[-1].uri if pl.segments else None)
        no_change = no_change + 1 if sig == last_sig and not failed else 0
        last_sig = sig
        if no_change >= s.hls_no_change_threshold:
            ctx.log.info("playlist unchanged for %d polls; capture finished", no_change)
            break
        await asyncio.sleep(s.hls_poll_interval_seconds)

    if not (ctx.hls_dir / "index.m3u8").exists():
        raise StepError("nothing was captured")
    ctx.payload["capture_done"] = True
    await ctx.save()


# ── Live stream recording ─────────────────────────────────────────────────


async def _live_variant(ctx: JobContext, login: str) -> str | None:
    tok = await ctx.deps.gql.live_access_token(login)
    try:
        master = await http.request("GET", hls.live_master_url(login, tok.value, tok.signature), attempts=2)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None  # offline
        raise
    variants = [v for v in hls.parse_master(master.text) if v.video != "audio_only"]
    if not variants:
        return None
    source = next((v for v in variants if v.video == "chunked"), None)
    return (source or max(variants, key=lambda v: v.bandwidth)).uri


def _load_entries(path: Path) -> list[tuple[str, float, bool]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            e = json.loads(line)
            out.append((e["name"], e["duration"], e["disc"]))
    return out


async def live_record(ctx: JobContext) -> None:
    """Record the live stream (pre-mute audio), skipping Twitch ad segments."""
    s = ctx.settings
    login = ctx.payload.get("login") or s.twitch_username
    d = ctx.hls_dir
    d.mkdir(parents=True, exist_ok=True)
    index_file = d / "segments.jsonl"
    last_seq: int = ctx.payload.get("last_seq", -1)
    pending_disc = last_seq >= 0  # resuming after a restart => there is a gap
    variant_url: str | None = None
    idle = 0
    failures = 0
    init_name: str | None = ctx.payload.get("init_name")
    last_save = time.monotonic()

    with index_file.open("a", encoding="utf-8") as index:
        while True:
            if variant_url is None:
                try:
                    variant_url = await _live_variant(ctx, login)
                except Exception as exc:
                    ctx.log.warning("live token/master failed: %s", exc)
                if variant_url is None:
                    idle += 1
                    if idle >= s.live_end_threshold // 10 + 1 and not await _still_live(ctx):
                        break
                    await asyncio.sleep(10)
                    continue
            try:
                text = (await http.request("GET", variant_url, attempts=2)).text
                failures = 0
            except httpx.HTTPError as exc:
                failures += 1
                ctx.log.info("live playlist fetch failed (%s); reconnecting", exc)
                variant_url = None
                pending_disc = True
                if failures > 30 and not await _still_live(ctx):
                    break
                await asyncio.sleep(s.live_poll_interval_seconds)
                continue

            pl = hls.parse_media(text)
            base = hls.base_url(variant_url)
            if pl.init_uri and init_name is None:
                init_name = "init" + Path(hls.local_name(pl.init_uri)).suffix
                await _download(_abs(base, pl.init_uri), d / init_name)
                ctx.payload["init_name"] = init_name

            new = [seg for seg in pl.segments if seg.sequence > last_seq]
            if new and last_seq >= 0 and new[0].sequence > last_seq + 1:
                pending_disc = True  # we fell behind the live window
            idle = 0 if new else idle + 1

            for seg in new:
                last_seq = seg.sequence
                if seg.ad:
                    pending_disc = True
                    continue
                ext = Path(hls.local_name(seg.uri)).suffix or ".ts"
                name = f"{seg.sequence:09d}{ext}"
                try:
                    await _download(_abs(base, seg.uri), d / name)
                except httpx.HTTPError as exc:
                    ctx.log.warning("live segment %s failed: %s", seg.sequence, exc)
                    pending_disc = True
                    continue
                index.write(json.dumps({"name": name, "duration": seg.duration, "disc": pending_disc}) + "\n")
                index.flush()
                pending_disc = False

            ctx.payload["last_seq"] = last_seq
            if time.monotonic() - last_save > 30:
                await ctx.save()
                last_save = time.monotonic()

            if pl.ended:
                break
            if idle >= s.live_end_threshold:
                if not await _still_live(ctx):
                    break
                idle = 0
            await asyncio.sleep(s.live_poll_interval_seconds)

    entries = _load_entries(index_file)
    if not entries:
        raise StepError("live recording captured no segments")
    playlist = hls.write_local_playlist(entries, init_name=init_name)
    (d / "index.m3u8").write_text(playlist, encoding="utf-8")
    ctx.payload["fmp4"] = bool(init_name)
    ctx.log.info("live recording finished: %d segments", len(entries))


async def _still_live(ctx: JobContext, stream_id: str | None = None) -> bool:
    """Is the channel live with ``stream_id`` (default: the job's stream)?"""
    helix = ctx.deps.helix
    if not helix.configured:
        return False
    stream = await helix.get_stream(ctx.settings.twitch_id)
    expected = stream_id if stream_id is not None else ctx.payload.get("stream_id")
    return bool(stream and str(stream.get("id")) == str(expected))
