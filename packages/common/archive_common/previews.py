"""Seek-bar previews: sprite sheets of an upload's frames, shared by the worker (makes them) and the API (serves them).

One upload (a YouTube video in ``vods.youtube``) gets sheets of ``COLS``×``ROWS`` tiles, each ``WIDTH``×``HEIGHT``,
in ``<previews_dir>/<youtube id>/<k>.jpg``. Frame ``i`` is the picture at ``i * INTERVAL`` seconds of the upload
(or the keyframe just before),
on sheet ``i // (COLS * ROWS)``, column ``i % COLS``, row ``(i // COLS) % ROWS``. The upload's entry carries
``preview`` (``info()``) once its sheets exist; the API serves them at ``/v1/previews/<youtube id>/<k>.jpg``.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

VERSION = 1
INTERVAL = 10  # seconds between frames
WIDTH, HEIGHT = 160, 90
COLS, ROWS = 10, 10

YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
SHEET = re.compile(r"^(\d{1,5})\.jpg$")


def frame_count(duration: float) -> int:
    """Frames of a ``duration``-second upload: one at 0 and one every ``INTERVAL`` seconds before its end."""
    return max(1, math.ceil(max(0.0, duration) / INTERVAL))


def sheet_count(frames: int) -> int:
    return math.ceil(frames / (COLS * ROWS))


def info(frames: int) -> dict[str, Any]:
    """The ``preview`` of an upload entry."""
    return {"v": VERSION, "interval": INTERVAL, "w": WIDTH, "h": HEIGHT, "cols": COLS, "rows": ROWS, "count": frames}


def directory(previews_dir: Path, youtube_id: str) -> Path:
    """An upload's sheets, under ``Settings.previews_dir``."""
    if not YOUTUBE_ID.match(youtube_id):
        raise ValueError(f"not a YouTube video id: {youtube_id!r}")
    return previews_dir / youtube_id
