"""What the worker keeps on disk, for the admin dashboard's storage view and cleanup.

Every job works in one folder (``JobContext.work_dir``): ``vods/<vod id>`` or, for a live
recording, ``live/<stream id>``. A folder is *stale* when no job for it is queued, running or
paused, and either no VOD row points at it or its last job failed or was cancelled: the
leftovers of a VOD that was deleted, or of a job nobody is going to retry.

Only names relative to the data directory ever leave this module.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from archive_common.db import get_sessionmaker
from archive_common.models import Vod

from .events import iso_utc
from .job_rows import ACTIVE, ALL_JOBS

AREAS = ("vods", "live")
NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
CACHE_SECONDS = 30


class StorageError(Exception):
    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status, self.msg = status, msg


@dataclass
class Folder:
    area: str
    name: str
    bytes: int
    files: int
    modified: float | None  # newest mtime inside, epoch seconds


def _size(path: Path) -> tuple[int, int, float | None]:
    """Bytes, files and newest change under ``path``, never following symlinks."""
    total = files = 0
    newest = None
    for root, dirs, names in os.walk(path, followlinks=False):
        for n, is_file in [(n, True) for n in names] + [(d, False) for d in dirs]:
            try:
                st = os.lstat(os.path.join(root, n))
            except OSError:
                continue  # removed while we looked
            newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
            if is_file:
                total += st.st_size
                files += 1
    return total, files, newest


def scan(data_dir: Path) -> list[Folder]:
    out = []
    for area in AREAS:
        base = data_dir / area
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                out.append(Folder(area, entry.name, *_size(entry)))
    return out


def folder_path(data_dir: Path, area: str, name: str) -> Path:
    """The folder ``<area>/<name>`` inside the data dir; StorageError for anything that could leave it."""
    if area not in AREAS:
        raise StorageError(404, f"No area {area}; areas: {', '.join(AREAS)}")
    if not NAME.match(name):
        raise StorageError(400, f"Not a folder name: {name}")
    base = (data_dir / area).resolve()
    path = data_dir / area / name
    if path.is_symlink() or path.resolve().parent != base:
        raise StorageError(400, f"{area}/{name} is not a folder of the data directory")
    if not path.is_dir():
        raise StorageError(404, f"No folder {area}/{name}")
    return path


def _jobs_of(area: str, names: list[str]) -> Any:
    """Jobs working in these folders (as JobContext.work_dir picks them)."""
    typ = func.coalesce(ALL_JOBS.c.payload["type"].astext, "vod")
    if area == "live":
        return (typ == "live") & ALL_JOBS.c.payload["stream_id"].astext.in_(names)
    return (typ != "live") & ALL_JOBS.c.vod_id.in_(names)


def _job_json(job: Any) -> dict:
    return {"id": job.id, "kind": job.kind, "state": job.state, "step": job.step,
            "updatedAt": iso_utc(job.updated_at)}


class Storage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._cache: tuple[float, list[Folder]] | None = None
        self._lock = asyncio.Lock()

    async def folders(self, refresh: bool = False) -> list[Folder]:
        async with self._lock:
            if refresh or self._cache is None or time.monotonic() - self._cache[0] > CACHE_SECONDS:
                self._cache = (time.monotonic(), await asyncio.to_thread(scan, self.data_dir))
            return self._cache[1]

    def disk(self) -> dict | None:
        try:
            usage = shutil.disk_usage(self.data_dir)
        except OSError:
            return None
        return {"total": usage.total, "used": usage.used, "free": usage.free}

    async def view(self, refresh: bool = False) -> dict:
        folders = await self.folders(refresh)
        rows = []
        async with get_sessionmaker()() as s:
            for area in AREAS:
                mine = [f for f in folders if f.area == area]
                if not mine:
                    continue
                names = [f.name for f in mine]
                key = Vod.stream_id if area == "live" else Vod.id
                vods = {}
                for v in (await s.execute(select(key, Vod.id, Vod.title, Vod.hidden).where(key.in_(names)))).all():
                    vods.setdefault(v[0], {"id": v.id, "title": v.title, "hidden": v.hidden})
                by_folder: dict[str, list[Any]] = {}
                stmt = select(ALL_JOBS).where(_jobs_of(area, names)).order_by(ALL_JOBS.c.id.desc())
                for job in (await s.execute(stmt)).all():
                    name = (job.payload or {}).get("stream_id") if area == "live" else job.vod_id
                    by_folder.setdefault(str(name), []).append(job)
                for f in mine:
                    jobs = by_folder.get(f.name, [])
                    active = [j for j in jobs if j.state in ACTIVE]
                    last = jobs[0] if jobs else None
                    vod = vods.get(f.name)
                    stale = not active and (vod is None or (last is not None and last.state in ("failed", "cancelled")))
                    rows.append({
                        "area": area, "name": f.name, "path": f"{area}/{f.name}", "bytes": f.bytes, "files": f.files,
                        "modifiedAt": iso_utc(dt.datetime.fromtimestamp(f.modified, dt.timezone.utc))
                        if f.modified is not None else None,
                        "vod": vod,
                        "jobs": {"active": [_job_json(j) for j in active], "last": _job_json(last) if last else None},
                        "stale": stale,
                    })
        return {"disk": self.disk(), "folders": rows, "cacheSeconds": CACHE_SECONDS}

    async def delete(self, area: str, name: str) -> dict:
        """Remove a folder no job is working in. Returns what was freed."""
        path = folder_path(self.data_dir, area, name)
        async with get_sessionmaker()() as s:
            active = (await s.execute(
                select(ALL_JOBS.c.id).where(_jobs_of(area, [name]), ALL_JOBS.c.state.in_(ACTIVE)).limit(1)
            )).scalar()
        if active is not None:
            raise StorageError(409, f"Job {active} is working in {area}/{name}; cancel it or let it finish first")
        size, files, _ = await asyncio.to_thread(_size, path)
        await asyncio.to_thread(shutil.rmtree, path)
        self._cache = None
        return {"path": f"{area}/{name}", "bytes": size, "files": files}
