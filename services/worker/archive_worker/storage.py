"""What the worker keeps on disk, for the admin dashboard's storage view and cleanup.

Every job works in one folder (``JobContext.work_dir``): ``vods/<vod id>`` or, for a live
recording, ``live/<stream id>``. A folder is *stale* when no job for it is queued, running or
paused, and either no VOD row points at it or its last job failed or was cancelled: the
leftovers of a VOD that was deleted, or of a job nobody is going to retry.

Deleting a folder moves it into ``<area>/.trash/`` at once and removes the files there in the
background: on the archive's disks removing a VOD's files can take minutes, longer than a proxy
waits for an answer. Whatever a restart left in a trash folder is removed when the worker starts.

Only names relative to the data directory ever leave this module.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from sqlalchemy import func, select

from .events import iso_utc
from .job_rows import ACTIVE, ALL_JOBS

log = logging.getLogger(__name__)

AREAS = ("vods", "live")
TRASH = ".trash"  # inside each area, so moving a folder there is a rename on the same disk
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
            if NAME.match(entry.name) and entry.is_dir() and not entry.is_symlink():  # not .trash
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


def _job_json(job: Any) -> dict[str, Any]:
    return {"id": job.id, "kind": job.kind, "state": job.state, "step": job.step, "updatedAt": iso_utc(job.updated_at)}


def to_trash(path: Path) -> Path:
    """Move a folder into its area's trash under a name no other delete uses, and return where it went."""
    trash = path.parent / TRASH
    trash.mkdir(exist_ok=True)
    target = trash / f"{path.name}-{time.time_ns()}"
    path.rename(target)
    return target


def trashed(data_dir: Path) -> list[Path]:
    """What earlier deletes left in the trash folders (a restart before the removal finished)."""
    return [p for area in AREAS if (data_dir / area / TRASH).is_dir() for p in (data_dir / area / TRASH).iterdir()]


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


class Storage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._cache: tuple[float, list[Folder]] | None = None
        self._sizes: dict[tuple[str, str], Folder] = {}  # the last scan's, kept when the cache is dropped
        self._lock = asyncio.Lock()
        self._removals: set[asyncio.Task[None]] = set()

    async def folders(self, refresh: bool = False) -> list[Folder]:
        async with self._lock:
            if refresh or self._cache is None or time.monotonic() - self._cache[0] > CACHE_SECONDS:
                found = await asyncio.to_thread(scan, self.data_dir)
                self._cache = (time.monotonic(), found)
                self._sizes = {(f.area, f.name): f for f in found}
            return self._cache[1]

    @property
    def removing(self) -> int:
        """Deleted folders whose files are still being removed."""
        return len(self._removals)

    def _remove_later(self, path: Path) -> None:
        async def run() -> None:
            try:
                await asyncio.to_thread(remove, path)
            except Exception:
                log.exception("storage: could not remove %s", path.relative_to(self.data_dir))
            self._cache = None  # the disk's free space changed

        task = asyncio.get_running_loop().create_task(run())
        self._removals.add(task)
        task.add_done_callback(self._removals.discard)

    def empty_trash(self) -> int:
        """Start removing what an earlier run left in the trash; the number of entries."""
        left = trashed(self.data_dir)
        for path in left:
            self._remove_later(path)
        return len(left)

    async def wait_removed(self) -> None:
        """Until every removal started so far has finished (tests, shutdown)."""
        while self._removals:
            await asyncio.gather(*list(self._removals), return_exceptions=True)

    def disk(self) -> dict[str, Any] | None:
        try:
            usage = shutil.disk_usage(self.data_dir)
        except OSError:
            return None
        return {"total": usage.total, "used": usage.used, "free": usage.free}

    async def view(self, refresh: bool = False) -> dict[str, Any]:
        folders = await self.folders(refresh)
        rows = []
        async with get_sessionmaker()() as s:
            for area in AREAS:
                mine = [f for f in folders if f.area == area]
                if not mine:
                    continue
                names = [f.name for f in mine]
                key = Vod.stream_id if area == "live" else Vod.id
                vods = {}  # type: ignore[var-annotated]
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
                    rows.append(
                        {
                            "area": area,
                            "name": f.name,
                            "path": f"{area}/{f.name}",
                            "bytes": f.bytes,
                            "files": f.files,
                            "modifiedAt": iso_utc(dt.datetime.fromtimestamp(f.modified, dt.UTC))
                            if f.modified is not None
                            else None,
                            "vod": vod,
                            "jobs": {
                                "active": [_job_json(j) for j in active],
                                "last": _job_json(last) if last else None,
                            },
                            "stale": stale,
                        }
                    )
        return {"disk": self.disk(), "folders": rows, "cacheSeconds": CACHE_SECONDS}

    async def delete(self, area: str, name: str) -> dict[str, Any]:
        """Delete a folder no job is working in: it is gone from the data directory when this returns, and
        its files are removed in the background. Returns what that frees."""
        path = folder_path(self.data_dir, area, name)
        async with get_sessionmaker()() as s:
            active = (
                await s.execute(
                    select(ALL_JOBS.c.id).where(_jobs_of(area, [name]), ALL_JOBS.c.state.in_(ACTIVE)).limit(1)
                )
            ).scalar()
        if active is not None:
            raise StorageError(409, f"Job {active} is working in {area}/{name}; cancel it or let it finish first")
        # The size the dashboard showed, from the last scan; only a folder it never saw is walked here.
        seen = self._sizes.pop((area, name), None)
        size, files = (seen.bytes, seen.files) if seen else (await asyncio.to_thread(_size, path))[:2]
        try:
            target = await asyncio.to_thread(to_trash, path)
        except OSError as exc:  # the trash can't be made, or the folder is its own mount: remove it where it is
            log.warning("storage: could not move %s/%s to the trash (%s); removing it in place", area, name, exc)
            target = path
        self._remove_later(target)
        self._cache = None
        return {"path": f"{area}/{name}", "bytes": size, "files": files}
