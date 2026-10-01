"""Worker settings changed from the admin dashboard (runtime_settings.py; the DB parts need the dev DB)."""

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, insert, select

from archive_common.db import get_sessionmaker
from archive_common.audit import AUDIT_LOG
from archive_common.models import RuntimeSetting
from archive_worker import jobs, legacy_jobs
from archive_worker.admin import create_admin_app
from archive_worker.context import JobContext
from archive_worker.runtime_settings import BY_KEY, RuntimeSettings, validate

KEY = {"Authorization": "Bearer k"}
TOUCHED = ("keep_hls", "split_duration", "restricted_games", "runner_concurrency", "manual_steps", "retired_key")


def test_validate():
    assert validate("keep_hls", True) is True
    assert validate("split_duration", 3600.0) == 3600 and isinstance(validate("split_duration", 3600.0), int)
    assert validate("youtube_keepalive_hours", 12) == 12.0
    assert validate("youtube_description", "") == ""
    assert validate("restricted_games", [" Artifact ", "Artifact", "Poker"]) == ["Artifact", "Poker"]
    assert validate("manual_steps", {"archive": ["upload", "upload"], "chat": []}) == {"archive": ["upload"]}


@pytest.mark.parametrize("key, value, msg", [
    ("data_dir", "/tmp", "can't be changed here"),
    ("twitch_client_secret", "x", "can't be changed here"),
    ("keep_hls", "yes", "true or false"),
    ("keep_hls", 1, "true or false"),
    ("split_duration", True, "must be a number"),
    ("split_duration", 3600.5, "whole number"),
    ("split_duration", 10, "between 600 and 43200"),
    ("runner_concurrency", 0, "between 1 and 16"),
    ("youtube_keepalive_hours", float("nan"), "must be a number"),
    ("youtube_description", "x" * 501, "at most 500"),
    ("restricted_games", "Artifact", "list of non-empty names"),
    ("restricted_games", ["ok", " "], "list of non-empty names"),
    ("manual_steps", ["upload"], "map job kinds"),
    ("manual_steps", {"archive": ["uplaod"]}, "no step"),
    ("manual_steps", {"nope": ["upload"]}, "unknown job kind"),
])
def test_validate_refused(key, value, msg):
    with pytest.raises(ValueError, match=msg):
        validate(key, value)


def test_no_secret_or_path_is_editable():
    for key in BY_KEY:
        assert not any(word in key for word in ("secret", "key", "url", "dir", "hash", "password", "database"))


def test_a_job_keeps_the_settings_it_started_with(deps):
    ctx = JobContext(1, "archive", "1", {}, deps)
    deps.settings.keep_hls = True
    assert ctx.settings.keep_hls is False
    assert JobContext(2, "archive", "1", {}, deps).settings.keep_hls is True


def test_apply_settings_reaches_the_runtime(deps):
    runtime = jobs.create_runtime(deps)
    assert (runtime.limiter.limit, runtime.max_attempts) == (3, deps.settings.max_attempts)
    deps.settings.runner_concurrency, deps.settings.max_attempts = 5, 7
    deps.settings.manual_steps = {"archive": ["upload"]}
    jobs.apply_settings(runtime, deps.settings)
    assert (runtime.limiter.limit, runtime.max_attempts) == (5, 7)
    assert runtime.registry.get("archive").pause_before == ("upload",)
    assert runtime.registry.get("download").pause_before == ()


def test_legacy_runner_reads_concurrency_each_time(deps):
    runner = legacy_jobs.Runner(deps, jobs.build_registry())
    assert runner.concurrency == 3
    deps.settings.runner_concurrency = 5
    assert runner.concurrency == 5
    assert legacy_jobs.Runner(deps, jobs.build_registry(), concurrency=1).concurrency == 1


# ── The table ─────────────────────────────────────────────────────────────


async def _clear():
    async with get_sessionmaker()() as s:
        await s.execute(delete(RuntimeSetting).where(RuntimeSetting.key.in_(TOUCHED)))
        await s.commit()


@pytest.fixture
async def clean(db):
    await _clear()
    async with get_sessionmaker()() as s:
        audit_after = (await s.execute(select(func.max(AUDIT_LOG.c.id)))).scalar() or 0
    yield audit_after
    await _clear()
    async with get_sessionmaker()() as s:
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.id > audit_after))
        await s.commit()


async def test_overrides_apply_and_reset(clean, settings):
    runtime = RuntimeSettings(settings)
    before, after = await runtime.update({"keep_hls": True, "split_duration": 3600}, "someone")
    assert before == {"keep_hls": False, "split_duration": 10800}
    assert after == {"keep_hls": True, "split_duration": 3600}
    assert (settings.keep_hls, settings.split_duration) == (True, 3600)

    # All or nothing: one refused value writes none of them.
    with pytest.raises(ValueError, match="between"):
        await runtime.update({"keep_hls": False, "runner_concurrency": 99}, "someone")
    assert settings.keep_hls is True

    # A fresh worker over the same env picks the overrides up; an unusable row is left out.
    async with get_sessionmaker()() as s:
        await s.execute(insert(RuntimeSetting).values(key="retired_key", value=1))
        await s.execute(insert(RuntimeSetting).values(key="restricted_games", value="not a list"))
        await s.commit()
    settings.keep_hls, settings.split_duration = False, 10800
    fresh = RuntimeSettings(settings)
    await fresh.load()
    assert (settings.keep_hls, settings.split_duration, settings.restricted_games) == (True, 3600, ["Artifact"])
    row = {r["key"]: r for r in fresh.describe()}["keep_hls"]
    assert row | {"updatedAt": None} == {
        "key": "keep_hls", "value": True, "default": False, "overridden": True, "type": "bool",
        "group": "Pipeline", "applies": "next job", "help": "Keep the HLS segments after upload",
        "min": None, "max": None, "updatedAt": None, "updatedBy": "someone",
    }

    assert await fresh.reset("keep_hls") == ({"keep_hls": True}, {"keep_hls": False})
    assert settings.keep_hls is False
    assert {r["key"]: r for r in fresh.describe()}["keep_hls"]["overridden"] is False
    with pytest.raises(KeyError):
        await fresh.reset("data_dir")


@pytest.fixture
def app(deps, clean):
    deps.settings.admin_api_key = SecretStr("k")
    service = jobs.JobService.create(deps)  # never opened: nothing here runs a job
    return create_admin_app(deps, service), service


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app[0]), base_url="https://admin")


async def _audit(after: int) -> list:
    """The settings rows audited since ``after``."""
    c = AUDIT_LOG.c
    async with get_sessionmaker()() as s:
        return (await s.execute(
            select(AUDIT_LOG).where(c.id > after, c.action.like("setting.%")).order_by(c.id)
        )).all()


async def test_settings_routes(app, clean, deps):
    limiter = app[1].runtime.limiter
    async with client(app) as c:
        assert (await c.get("/admin/settings")).status_code == 403
        rows = (await c.get("/admin/settings", headers=KEY)).json()["data"]
        assert [r["key"] for r in rows] == list(BY_KEY)
        steps = next(r for r in rows if r["key"] == "manual_steps")
        assert steps["choices"]["archive"] == jobs.KINDS["archive"]

        r = await c.patch("/admin/settings", headers=KEY, json={"runner_concurrency": 6, "keep_hls": "yes"})
        assert r.status_code == 400 and "true or false" in r.json()["msg"]
        assert limiter.limit == 3
        r = await c.patch("/admin/settings", headers=KEY, json={})
        assert r.status_code == 400

        r = await c.patch("/admin/settings", headers=KEY,
                          json={"runner_concurrency": 6, "manual_steps": {"archive": ["upload"]}})
        assert r.status_code == 200
        row = {x["key"]: x for x in r.json()["data"]}["runner_concurrency"]
        assert (row["value"], row["default"], row["overridden"], row["updatedBy"]) == (6, 3, True, "api-key")
        assert limiter.limit == 6 and app[1].legacy.concurrency == 6
        kinds = (await c.get("/admin/kinds", headers=KEY)).json()
        assert kinds["archive"]["manualSteps"] == ["upload"]

        r = await c.delete("/admin/settings/runner_concurrency", headers=KEY)
        assert r.status_code == 200 and limiter.limit == 3
        assert (await c.delete("/admin/settings/data_dir", headers=KEY)).status_code == 404

    patch, reset = await _audit(clean)
    assert (patch.action, patch.target, patch.actor_kind) == ("setting.update", None, "api_key")
    assert (patch.before, patch.after) == ({"runner_concurrency": 3, "manual_steps": {}},
                                           {"runner_concurrency": 6, "manual_steps": {"archive": ["upload"]}})
    assert (reset.action, reset.target) == ("setting.reset", "setting:runner_concurrency")
    assert (reset.before, reset.after) == ({"runner_concurrency": 6}, {"runner_concurrency": 3})
