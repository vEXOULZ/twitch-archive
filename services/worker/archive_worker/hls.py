"""Minimal HLS playlist handling tailored to Twitch.

Covers exactly what the worker needs: master-playlist variants, media-playlist
segments (with EXT-X-MAP init sections, Twitch's total-seconds tag and
stitched-ad markers), variant selection with the chunked -> 1080p fallback,
and writing a local playlist that ffmpeg can read.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

USHER = "https://usher.ttvnw.net"

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attrs(s: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _ATTR_RE.findall(s)}


# ── Master playlists ──────────────────────────────────────────────────────


@dataclass
class Variant:
    uri: str
    bandwidth: int = 0
    height: int | None = None
    video: str | None = None  # GROUP-ID, "chunked" for source
    name: str | None = None


def parse_master(text: str) -> list[Variant]:
    names: dict[str, str] = {}
    variants: list[Variant] = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            a = parse_attrs(line.split(":", 1)[1])
            if a.get("GROUP-ID") and a.get("NAME"):
                names[a["GROUP-ID"]] = a["NAME"]
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attrs(line.split(":", 1)[1])
        elif not line.startswith("#") and pending is not None:
            res = pending.get("RESOLUTION", "")
            height = int(res.split("x")[1]) if "x" in res else None
            video = pending.get("VIDEO")
            variants.append(
                Variant(
                    uri=line,
                    bandwidth=int(pending.get("BANDWIDTH", "0") or 0),
                    height=height,
                    video=video,
                    name=names.get(video or ""),
                )
            )
            pending = None
    return variants


def twitch_variant_candidates(variants: list[Variant]) -> list[str]:
    """Chunked (source) variant first, then the 1080p transcode (upstream 801d597c).

    If no chunked variant is listed, derive its URL from the first variant:
    ``https://host/<hash>/<quality>/index-dvr.m3u8`` -> ``.../<hash>/chunked/index-dvr.m3u8``.
    """
    out: list[str] = []

    def push(url: str | None) -> None:
        if url and url not in out:
            out.append(url)

    chunked = next((v for v in variants if "/chunked/" in v.uri or v.video == "chunked"), None)
    if chunked:
        push(chunked.uri)
    elif variants:
        first = variants[0].uri
        m = re.match(r"(https?://[^/]+)/([^/]+)/[^/]+/(index-[^/]+)$", first)
        push(f"{m.group(1)}/{m.group(2)}/chunked/{m.group(3)}" if m else first)
    hd = next((v for v in variants if "/chunked/" not in v.uri and v.height == 1080), None)
    push(hd.uri if hd else None)
    if not out and variants:
        push(variants[0].uri)
    return out


def vod_master_url(vod_id: str, token: str, sig: str) -> str:
    p = random.randint(1_000_000, 9_999_999)
    return (
        f"{USHER}/vod/v2/{vod_id}.m3u8?allow_source=true&player=mediaplayer&include_unavailable=true"
        f"&supported_codecs={quote('av1,h265,h264')}&playlist_include_framerate=true&allow_spectre=true"
        f"&nauthsig={sig}&nauth={quote(token)}&platform=web&p={p}&transcode_mode=cbr_v1"
    )


def live_master_url(login: str, token: str, sig: str) -> str:
    p = random.randint(1_000_000, 9_999_999)
    return (
        f"{USHER}/api/channel/hls/{login.lower()}.m3u8?allow_source=true&allow_audio_only=true"
        f"&fast_bread=true&player_backend=mediaplayer&playlist_include_framerate=true"
        f"&reassignments_supported=true&supported_codecs={quote('av1,h265,h264')}"
        f"&sig={sig}&token={quote(token)}&platform=web&p={p}&transcode_mode=cbr_v1"
    )


def base_url(url: str) -> str:
    return url[: url.rfind("/")]


# ── Media playlists ───────────────────────────────────────────────────────


@dataclass
class Segment:
    uri: str
    duration: float
    title: str = ""
    sequence: int = 0
    discontinuity: bool = False
    ad: bool = False
    program_date_time: str | None = None


@dataclass
class MediaPlaylist:
    segments: list[Segment] = field(default_factory=list)
    target_duration: int = 10
    media_sequence: int = 0
    init_uri: str | None = None
    ended: bool = False
    total_seconds: float | None = None
    version: int = 3



def parse_media(text: str) -> MediaPlaylist:
    pl = MediaPlaylist()
    duration: float | None = None
    title = ""
    disc = False
    pdt: str | None = None
    ad_ranges: list[tuple[str, float]] = []  # (start date, duration) of stitched ads
    seq = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            pl.target_duration = int(float(line.split(":", 1)[1]))
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            pl.media_sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-VERSION:"):
            pl.version = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MAP:"):
            pl.init_uri = parse_attrs(line.split(":", 1)[1]).get("URI")
        elif line.startswith("#EXT-X-TWITCH-TOTAL-SECS:"):
            pl.total_seconds = float(line.split(":", 1)[1])
        elif line == "#EXT-X-ENDLIST":
            pl.ended = True
        elif line == "#EXT-X-DISCONTINUITY":
            disc = True
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pdt = line.split(":", 1)[1]
        elif line.startswith("#EXT-X-DATERANGE:"):
            a = parse_attrs(line.split(":", 1)[1])
            if a.get("CLASS") == "twitch-stitched-ad" or a.get("ID", "").startswith("stitched-ad-"):
                ad_ranges.append((a.get("START-DATE", ""), float(a.get("DURATION", "0") or 0)))
        elif line.startswith("#EXTINF:"):
            val = line.split(":", 1)[1]
            dur, _, title = val.partition(",")
            duration = float(dur)
        elif not line.startswith("#") and duration is not None:
            if seq is None:
                seq = pl.media_sequence
            is_ad = "Amazon" in title or title.startswith("stitched-ad")
            if not is_ad and pdt and ad_ranges:
                is_ad = any(_in_range(pdt, start, d) for start, d in ad_ranges)
            pl.segments.append(
                Segment(
                    uri=line,
                    duration=duration,
                    title=title,
                    sequence=seq,
                    discontinuity=disc,
                    ad=is_ad,
                    program_date_time=pdt,
                )
            )
            seq += 1
            duration, title, disc, pdt = None, "", False, None
    return pl


def _in_range(pdt: str, start: str, duration: float) -> bool:
    from datetime import datetime, timedelta

    try:
        t = datetime.fromisoformat(pdt.replace("Z", "+00:00"))
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return False
    return s <= t < s + timedelta(seconds=duration)


# ── Muted segments ────────────────────────────────────────────────────────

_MUTED_RE = re.compile(r"-(?:un)?muted(?=\.[a-z0-9]+$)")


def unmuted_name(uri: str) -> str:
    """'12-muted.ts' / '12-unmuted.ts' -> '12.ts'."""
    return _MUTED_RE.sub("", uri)


def local_name(uri: str) -> str:
    """File name for a segment URI (drop any query string / path)."""
    path = urlparse(uri).path if "://" in uri else uri.split("?", 1)[0]
    return path.rsplit("/", 1)[-1]


# ── Writing local playlists ───────────────────────────────────────────────


def write_local_playlist(
    entries: list[tuple[str, float, bool]], *, init_name: str | None = None, target_duration: int = 10
) -> str:
    """entries: (local file name, duration, discontinuity-before)."""
    version = 7 if init_name else 3
    lines = [
        "#EXTM3U",
        f"#EXT-X-VERSION:{version}",
        f"#EXT-X-TARGETDURATION:{max(target_duration, int(max((d for _, d, _ in entries), default=0)) + 1)}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    if init_name:
        lines.append(f'#EXT-X-MAP:URI="{init_name}"')
    for name, dur, disc in entries:
        if disc:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{dur:.3f},")
        lines.append(name)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
