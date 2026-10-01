"""A synthetic VOD's segments (``vod_segments``), as both the worker and archive-api read them.

A segment is the window ``[start, end)`` of a real VOD (its *source*), placed at ``at`` seconds on the
synthetic VOD's timeline. ``end`` None runs to the source's end, so a source that grows (a re-finalized
capture) is followed. Whatever is stored, a segment never runs past its source's end, nor past where
the next segment starts: ``resolve`` gives each one's actual end, which is what everything plays.

A source may itself be a synthetic VOD (a playthrough of a merged stream, a merge of merges), up to
``MAX_DEPTH`` deep. What reads segments to play them wants real VODs only: ``flatten`` turns the
windows of synthetic sources into the windows of real VODs they cover, and numbers the streams.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

EPS = 0.001  # float noise tolerated where two times should meet
MAX_DEPTH = 3  # synthetic VODs of synthetic VODs: at most this many levels below the top one


def seconds(value: Any) -> int | float:
    """A stored number of seconds as JSON shows it: whole seconds as ints, else to the millisecond."""
    v = round(float(value), 3)
    return int(v) if v.is_integer() else v


@dataclass(frozen=True)
class Segment:
    source_id: str
    start: float
    end: float | None
    at: float
    label: str | None = None
    # Which stream of the synthetic VOD it plays, from 0 (``flatten`` sets it; stored segments have none).
    stream: int | None = None

    @classmethod
    def of_row(cls, row: Any) -> Segment:
        """From a ``VodSegment`` (or a row with its columns)."""
        return cls(row.source_id, float(row.start_s), None if row.end_s is None else float(row.end_s),
                   float(row.at_s), row.label)

    @property
    def length(self) -> float:
        return max(0.0, (self.end or 0.0) - self.start)

    def columns(self) -> dict[str, Any]:
        """``vod_segments`` values (without ``vod_id`` and ``pos``)."""
        return {"source_id": self.source_id, "start_s": Decimal(str(seconds(self.start))),
                "end_s": None if self.end is None else Decimal(str(seconds(self.end))),
                "at_s": Decimal(str(seconds(self.at))), "label": self.label}

    def json(self) -> dict[str, Any]:
        out = {"vodId": self.source_id, "start": seconds(self.start),
               "end": None if self.end is None else seconds(self.end), "at": seconds(self.at), "label": self.label}
        if self.stream is not None:
            out["stream"] = self.stream
        return out


def resolve(segments: Iterable[Segment], durations: Mapping[str, float]) -> list[Segment]:
    """``segments`` (in ``at`` order), each with ``end`` set to where it actually ends in its source:
    its own ``end``, else the source's end, but never past the source's end (``durations``; unknown or
    0 = not limited) nor past the next segment's ``at``. A segment left with nothing has ``end == start``."""
    segments = list(segments)
    out = []
    for i, seg in enumerate(segments):
        limits = [seg.end] if seg.end is not None else []
        if durations.get(seg.source_id):
            limits.append(float(durations[seg.source_id]))
        if i + 1 < len(segments):
            limits.append(seg.start + segments[i + 1].at - seg.at)
        end = min(limits) if limits else seg.start
        out.append(replace(seg, end=max(seg.start, end)))
    return out


def total(resolved: Iterable[Segment]) -> float:
    """Where the last of the resolved segments ends on the synthetic timeline."""
    return max((s.at + s.length for s in resolved), default=0.0)


def flatten(top: Iterable[Segment], inner: Mapping[str, list[Segment]], supersedes: Mapping[str, bool],
            one_stream: bool = False, _depth: int = 0) -> list[Segment]:
    """``top`` (resolved) as windows of real VODs only, each with its ``stream``.

    A segment whose source is a synthetic VOD (a key of ``inner``, which holds every synthetic VOD's
    resolved segments) becomes the parts of that VOD's own (flattened) segments its window covers,
    placed where the window is; its ``label`` wins over theirs. Streams are numbered in order of first
    appearance: a new one wherever the source changes from the segment before (as the site does when
    told nothing); a merge or a split (``supersedes``) is one broadcast, so all of it is that one
    stream, while a playthrough's streams stay apart. ``one_stream``: ``top`` is such a broadcast itself,
    so everything is stream 0. Deeper than ``MAX_DEPTH`` is a ValueError."""
    if _depth > MAX_DEPTH:
        raise ValueError(f"synthetic VODs are nested more than {MAX_DEPTH} deep")
    out: list[Segment] = []
    numbers: dict[tuple, int] = {}
    run, prev = -1, None
    for seg in top:
        if seg.source_id != prev and not (one_stream and run == 0):
            run += 1
        prev = seg.source_id
        if seg.source_id not in inner:
            out.append(replace(seg, stream=numbers.setdefault((run,), len(numbers))))
            continue
        end = seg.start + seg.length
        for part in flatten(inner[seg.source_id], inner, supersedes, _depth=_depth + 1):
            a, b = max(seg.start, part.at), min(end, part.at + part.length)
            if b - a <= EPS:
                continue
            key = (run,) if supersedes.get(seg.source_id) else (run, part.stream)
            out.append(Segment(part.source_id, part.start + a - part.at, part.start + b - part.at,
                               seg.at + a - seg.start, seg.label or part.label, numbers.setdefault(key, len(numbers))))
    return out
