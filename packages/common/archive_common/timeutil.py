"""Timestamp and duration helpers shared by the API and worker."""

from __future__ import annotations

import datetime as dt


def parse_ts(value: str | None) -> dt.datetime | None:
    """ISO-8601 (including a trailing 'Z') -> datetime; None for empty input."""
    return dt.datetime.fromisoformat(value) if value else None


def parse_helix_duration(value: str) -> int:
    """Helix durations look like '3h2m1s'."""
    total = 0
    num = ""
    for ch in value or "":
        if ch.isdigit():
            num += ch
            continue
        if num:
            total += int(num) * {"h": 3600, "m": 60, "s": 1}.get(ch, 0)
        num = ""
    return total


def format_hhmmss(seconds: float) -> str:
    """Always zero-padded HH:MM:SS (hours may exceed 99 for very long streams)."""
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def hhmmss_to_seconds(value: str | None) -> int:
    total = 0
    for piece in (value or "0").split(":"):
        total = total * 60 + int(float(piece or 0))
    return total
