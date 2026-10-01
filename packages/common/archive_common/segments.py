"""A synthetic VOD's segments (``vod_segments``), as both the worker and archive-api read them.

A segment is the window ``[start, end)`` of a real VOD (its *source*), placed at ``at`` seconds on the
synthetic VOD's timeline. ``end`` None runs to the source's end, so a source that grows (a re-finalized
capture) is followed. Whatever is stored, a segment never runs past its source's end, nor past where
the next segment starts: ``resolve`` gives each one's actual end, which is what everything plays.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

EPS = 0.001  # float noise tolerated where two times should meet


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
        return {"vodId": self.source_id, "start": seconds(self.start),
                "end": None if self.end is None else seconds(self.end), "at": seconds(self.at), "label": self.label}


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
