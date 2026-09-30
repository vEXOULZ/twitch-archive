"""The admin storage view and cleanup (storage.py; the route tests need the dev DB)."""

import datetime as dt
import os

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select

from archive_common.db import get_sessionmaker
from archive_common.models import AdminAudit, Job, Vod
from archive_worker import jobs
from archive_worker.admin import create_admin_app
from archive_worker.storage import StorageError, folder_path, scan

KEY = {"Authorization": "Bearer k"}
KEPT, FAILED, ORPHAN = "test-storage-kept", "test-storage-failed", "test-storage-orphan"
STREAM = "999000000777"


def put(path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_scan_counts_files_and_bytes(tmp_path):
    put(tmp_path / "vods" / "1" / "1.mp4", 100)
    put(tmp_path / "vods" / "1" / "hls" / "0.ts", 20)
    put(tmp_path / "live" / "2" / "2.mp4", 5)
    put(tmp_path / "vods" / "stray-file", 1)  # not a folder: left out
    (tmp_path / "other").mkdir()  # not an area
    got = {(f.area, f.name): (f.bytes, f.files) for f in scan(tmp_path)}
    assert got == {("vods", "1"): (120, 2), ("live", "2"): (5, 1)}


@pytest.mark.parametrize("area, name, status", [
    ("vods", "..", 400), ("vods", ".", 400), ("vods", "a b", 400), ("other", "1", 404), ("vods", "missing", 404),
])
def test_folder_path_refused(tmp_path, area, name, status):
    (tmp_path / "vods").mkdir()
    with pytest.raises(StorageError) as err:
        folder_path(tmp_path, area, name)
    assert err.value.status == status


def test_a_symlink_out_of_the_data_dir_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "data" / "vods").mkdir(parents=True)
    try:
        os.symlink(outside, tmp_path / "data" / "vods" / "link", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not allowed here")
    with pytest.raises(StorageError, match="not a folder of the data directory"):
        folder_path(tmp_path / "data", "vods", "link")
    assert [f.name for f in scan(tmp_path / "data")] == []


async def _reset(audit_after: int | None = None):
    async with get_sessionmaker()() as s:
        await s.execute(delete(Job).where(Job.vod_id.in_([KEPT, FAILED, ORPHAN])))
        await s.execute(delete(Vod).where(Vod.id.in_([KEPT, FAILED])))
        if audit_after is not None:
            await s.execute(delete(AdminAudit).where(AdminAudit.id > audit_after))
        await s.commit()


@pytest.fixture
async def world(db, settings):
    await _reset()
    async with get_sessionmaker()() as s:
        audit_after = (await s.execute(select(func.max(AdminAudit.id)))).scalar() or 0
        now = dt.datetime.now(dt.timezone.utc)
        s.add_all([
            Vod(id=KEPT, title="kept", created_at=now, duration="01:00:00", stream_id=STREAM),
            Vod(id=FAILED, title="failed", created_at=now, duration="01:00:00", hidden=True),
            Job(vod_id=FAILED, kind="archive", state="done", payload={}),
            Job(vod_id=KEPT, kind="live", state="running", step="live_record",
                payload={"type": "live", "stream_id": STREAM}),
        ])
        await s.flush()
        s.add(Job(vod_id=FAILED, kind="reupload", state="failed", payload={}))
        await s.commit()
    for name, size in ((KEPT, 10), (FAILED, 20), (ORPHAN, 30)):
        put(settings.data_dir / "vods" / name / f"{name}.mp4", size)
    put(settings.data_dir / "live" / STREAM / "hls" / "0.ts", 7)
    yield audit_after
    await _reset(audit_after)


@pytest.fixture
def app(deps):
    deps.settings.admin_api_key = SecretStr("k")
    return create_admin_app(deps, jobs.JobService.create(deps))


async def test_storage_view_and_delete(world, app, settings):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        assert (await c.get("/admin/storage")).status_code == 403
        body = (await c.get("/admin/storage", headers=KEY)).json()
        assert set(body["disk"]) == {"total", "used", "free"} and body["cacheSeconds"] == 30
        rows = {f["path"]: f for f in body["folders"]}
        assert str(settings.data_dir) not in str(body)

        kept, failed, orphan, live = (rows[f"vods/{KEPT}"], rows[f"vods/{FAILED}"], rows[f"vods/{ORPHAN}"],
                                      rows[f"live/{STREAM}"])
        assert (kept["bytes"], kept["files"], kept["vod"], kept["stale"]) == (
            10, 1, {"id": KEPT, "title": "kept", "hidden": False}, False)
        assert kept["jobs"] == {"active": [], "last": None}  # the live job works in live/<stream>
        assert (failed["vod"]["hidden"], failed["jobs"]["last"]["state"], failed["stale"]) == (True, "failed", True)
        assert (orphan["vod"], orphan["stale"]) == (None, True)
        assert live["vod"]["id"] == KEPT and live["stale"] is False
        assert [j["kind"] for j in live["jobs"]["active"]] == ["live"]

        r = await c.delete(f"/admin/storage/live/{STREAM}", headers=KEY)
        assert r.status_code == 409 and "cancel it" in r.json()["msg"]
        assert (settings.data_dir / "live" / STREAM).exists()
        assert (await c.delete("/admin/storage/vods/a%20b", headers=KEY)).status_code == 400
        assert (await c.delete("/admin/storage/nope/x", headers=KEY)).status_code == 404

        # Cached until refreshed; a delete drops the cache.
        put(settings.data_dir / "vods" / ORPHAN / "more.bin", 5)
        assert {f["path"]: f for f in (await c.get("/admin/storage", headers=KEY)).json()["folders"]}[
            f"vods/{ORPHAN}"]["bytes"] == 30
        r = await c.delete(f"/admin/storage/vods/{ORPHAN}", headers=KEY)
        assert r.status_code == 200 and r.json() == {"path": f"vods/{ORPHAN}", "bytes": 35, "files": 2}
        assert not (settings.data_dir / "vods" / ORPHAN).exists()
        paths = [f["path"] for f in (await c.get("/admin/storage", headers=KEY)).json()["folders"]]
        assert f"vods/{ORPHAN}" not in paths and f"vods/{KEPT}" in paths

    async with get_sessionmaker()() as s:
        row = (await s.execute(select(AdminAudit).where(AdminAudit.id > world))).scalar_one()
    assert (row.action, row.target) == ("DELETE /admin/storage/{area}/{name}", f"storage:vods/{ORPHAN}")
    assert row.detail == {"path": f"vods/{ORPHAN}", "bytes": 35, "files": 2}
