"""How the site shows VOD tags: the worker's admin routes save them, archive-api serves them (the routes
need the dev DB)."""

import datetime as dt

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select

from archive_common.audit import AUDIT_LOG
from archive_common.db import get_sessionmaker
from archive_common.models import SiteSetting, SiteTagShape, Vod
from archive_worker import jobs
from archive_worker.admin import create_admin_app
from archive_worker.site_tags import SiteTagError, is_color, validate

KEY = {"Authorization": "Bearer k"}
SVG = {"content-type": "image/svg+xml", **KEY}
STAR = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        b'<path d="M12 2L15 9H22L16 14L18 21L12 17L6 21L8 14L2 9H9Z"/></svg>')


def tag(name: str, **fields) -> dict:
    return {"name": name, "label": name.title(), "drawn": False, "color": None, "width": None, "height": None,
            **fields}


AUTO = [tag("new"), tag("updated"), tag("compilation")]


# ── Validation ────────────────────────────────────────────────────────────


def test_validate_keeps_order_and_known_fields():
    body = {"tags": [tag("speedrun", label="Speedrun!", drawn=True, color="#ff8800cc", width=24, height=200,
                         shape="v1/site/tags/speedrun.svg?v=x", extra=1), *AUTO]}
    out = validate(body)
    assert [t["name"] for t in out] == ["speedrun", "new", "updated", "compilation"]
    assert out[0] == {"name": "speedrun", "label": "Speedrun!", "drawn": True, "color": "#ff8800cc",
                      "width": 24, "height": 200, **dict.fromkeys(TEXT + PATTERN)}


TEXT = ("text", "textColor", "textSize", "textX", "textY", "textRotate")
PATTERN = ("pattern", "patternColor", "patternSize")


def test_text_and_pattern_kept():
    fields = {"text": "  100%  ", "textColor": "white", "textSize": 6, "textX": -100, "textY": 100,
              "textRotate": -180, "pattern": "checks", "patternColor": "var(--vx-bg)", "patternSize": 40}
    [out, *_] = validate({"tags": [tag("x", **fields), *AUTO]})
    assert list(out) == ["name", "label", "drawn", "color", "width", "height", *TEXT, *PATTERN]
    assert out == {**tag("x", **fields), "text": "100%"}  # trimmed
    [out, *_] = validate({"tags": [tag("x", pattern="stripes", textRotate=180, textSize=48), *AUTO]})
    assert (out["pattern"], out["textRotate"]) == ("stripes", None)


def test_without_text_or_pattern_the_rest_is_dropped():
    fields = {"text": None, "textColor": "red", "textSize": 12, "textX": 1, "textY": 2, "textRotate": 90,
              "pattern": None, "patternColor": "blue", "patternSize": 4}
    [out, *_] = validate({"tags": [tag("x", **fields), *AUTO]})
    assert all(out[k] is None for k in TEXT + PATTERN)


@pytest.mark.parametrize("color", [
    None, "#abc", "#abcd", "#aabbcc", "#aabbccdd", "var(--vx-accent)", "red", "rebeccapurple", "currentColor",
    "rgb(1, 2, 3)", "rgba(1 2 3 / 50%)", "hsl(120deg 50% 50%)", "oklch(70% 0.1 200)", "hwb(120 10% 20%)",
    "oklch(from var(--vx-accent) calc(l - 0.15) c h)", "color-mix(in oklch, var(--vx-ok) 60%, white)",
    "color(display-p3 1 0 0)", "rgb(calc(255 * 0.5) max(1, 2) clamp(0, 3, 9))",
    "color-mix(in srgb, color-mix(in srgb, red, blue), rgb(calc(min(1, 2))))",  # 4 deep
    "RGB(1 2 3)", "VAR(--vx-accent)",
])
def test_colors_allowed(color):
    for field in ("color", "textColor", "patternColor"):
        validate({"tags": [tag("x", text="t", pattern="stripes", **{field: color}), *AUTO]})


@pytest.mark.parametrize("color", [
    "#ab", "#abcdef012", "re", "", " red", "red\n", "red; background: blue", "url(http://x)", "image-set(x)",
    "rgb(1;x:expression(1))", "rgb(1, 2, 3", "rgb(1, 2, 3))", "rgb(1)(2)", "rgb(1) red", "red rgb(1)",
    "calc(1 + 2)", "var(--other)", "var(--vx-a, red)", "rgb(var(--x))", "rgb(1 -- 2)", "rgb(v(1))",
    "rgb(" + "1" * 156 + ")", "color-mix(in srgb, rgb(calc(min(max(1)))), red)",  # 5 deep
    "rgb(1 2 3 / 'x')", "ſſſ", "rgb(1 2 3)\n", 5, True, ["red"],
])
def test_colors_refused(color):
    assert not is_color(color)
    for field in ("color", "textColor", "patternColor"):
        with pytest.raises(SiteTagError) as err:
            validate({"tags": [tag("x", text="t", pattern="checks", **{field: color}), *AUTO]})
        assert err.value.field == field


@pytest.mark.parametrize("fields, field", [
    ({"name": "Upper"}, "name"),
    ({"name": "-dash"}, "name"),
    ({"name": "a" * 33}, "name"),
    ({"name": 5}, "name"),
    ({"name": "ok\n"}, "name"),
    ({"label": ""}, "label"),
    ({"label": "x" * 41}, "label"),
    ({"label": None}, "label"),
    ({"drawn": "yes"}, "drawn"),
    ({"drawn": None}, "drawn"),
    ({"color": "#ggg"}, "color"),
    ({"color": "var(--other)"}, "color"),
    ({"color": "re"}, "color"),
    ({"color": "url(http://x)"}, "color"),
    ({"color": "rgb(1;x:expression(1))"}, "color"),
    ({"color": "red; background: blue"}, "color"),
    ({"width": 7}, "width"),
    ({"width": 201}, "width"),
    ({"width": 24.5}, "width"),
    ({"width": True}, "width"),
    ({"height": "24"}, "height"),
    ({"text": ""}, "text"),
    ({"text": "   "}, "text"),
    ({"text": "x" * 25}, "text"),
    ({"text": 5}, "text"),
    ({"text": "t", "textSize": 5}, "textSize"),
    ({"text": "t", "textSize": 49}, "textSize"),
    ({"text": "t", "textX": -101}, "textX"),
    ({"text": "t", "textY": 101}, "textY"),
    ({"text": "t", "textY": 1.5}, "textY"),
    ({"text": "t", "textRotate": 181}, "textRotate"),
    ({"text": "t", "textRotate": False}, "textRotate"),
    ({"text": "t", "textColor": "url(x)"}, "textColor"),
    ({"textColor": "url(x)"}, "textColor"),  # checked even when dropped
    ({"pattern": "dots"}, "pattern"),
    ({"pattern": ""}, "pattern"),
    ({"pattern": "stripes", "patternSize": 1}, "patternSize"),
    ({"pattern": "stripes", "patternSize": 41}, "patternSize"),
    ({"pattern": "stripes", "patternColor": "var(--x)"}, "patternColor"),
])
def test_refused_field_named(fields, field):
    with pytest.raises(SiteTagError) as err:
        validate({"tags": [*AUTO, {**tag("speedrun"), **fields}]})
    assert (err.value.status, err.value.field) == (400, field)
    named = fields.get("name", "speedrun")
    assert err.value.tag == (named if isinstance(named, str) else None)
    assert err.value.msg.startswith("tags[3]") and f".{field}:" in err.value.msg


@pytest.mark.parametrize("missing", ["new", "updated", "compilation"])
def test_auto_tags_required(missing):
    with pytest.raises(SiteTagError, match=missing) as err:
        validate({"tags": [t for t in AUTO if t["name"] != missing]})
    assert (err.value.tag, err.value.field) == (missing, "name")


@pytest.mark.parametrize("body", [None, [], {"tags": "x"}, {"tags": [*AUTO, "x"]}, {}])
def test_not_a_list_of_tags(body):
    with pytest.raises(SiteTagError):
        validate(body)


def test_limits():
    validate({"tags": [*AUTO, *(tag(f"t{i}") for i in range(29))]})  # 32
    with pytest.raises(SiteTagError, match="at most 32"):
        validate({"tags": [*AUTO, *(tag(f"t{i}") for i in range(30))]})
    with pytest.raises(SiteTagError, match="listed twice") as err:
        validate({"tags": [*AUTO, tag("new")]})
    assert (err.value.tag, err.value.field) == ("new", "name")


# ── The routes ────────────────────────────────────────────────────────────


async def _clear():
    async with get_sessionmaker()() as s:
        await s.execute(delete(SiteTagShape))
        await s.execute(delete(SiteSetting).where(SiteSetting.key == "tags"))
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


@pytest.fixture
def app(deps, clean):
    deps.settings.admin_api_key = SecretStr("k")
    deps.settings.admin_password = SecretStr("pw")
    return create_admin_app(deps, jobs.JobService.create(deps))  # never opened: nothing here runs a job


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin")


async def _audit(after: int) -> list:
    c = AUDIT_LOG.c
    async with get_sessionmaker()() as s:
        return (await s.execute(
            select(AUDIT_LOG).where(c.id > after, c.action.like("site.%")).order_by(c.id)
        )).all()


async def test_never_saved(app, api, clean):
    async with client(app) as c:
        assert (await c.get("/admin/site/tags")).status_code == 403
        assert (await c.get("/admin/site/tags", headers=KEY)).json() == {"tags": [], "updatedAt": None,
                                                                         "updatedBy": None}
        r = await c.put("/admin/site/tags/new/shape", headers=SVG, content=STAR)
        assert r.status_code == 404 and r.json()["error"] is True
    async with api:
        assert (await api.get("/v1/site/tags")).status_code == 404
        assert (await api.get("/v1/site/tags/new.svg")).status_code == 404


async def test_save_shape_and_serve(app, api, clean):
    tags = [tag("speedrun", drawn=True, color="var(--vx-gold)", width=20, height=20), *AUTO]
    async with client(app) as c:
        r = await c.put("/admin/site/tags", headers=KEY, json={"tags": tags})
        assert r.status_code == 200, r.text
        body = r.json()
        assert [t["name"] for t in body["tags"]] == ["speedrun", "new", "updated", "compilation"]
        assert body["updatedBy"] == "api-key" and body["updatedAt"]
        assert list(body["tags"][0]) == ["name", "label", "drawn", "color", "shape", "width", "height", *TEXT, *PATTERN]
        assert body["tags"][0]["shape"] is None
        assert (await c.get("/admin/site/tags", headers=KEY)).json() == body

        r = await c.put("/admin/site/tags/speedrun/shape", headers=SVG, content=STAR)
        assert r.status_code == 200, r.text
        shape = r.json()["tags"][0]["shape"]
        assert shape.startswith("v1/site/tags/speedrun.svg?v=") and len(shape.split("=")[1]) == 16

    async with api:
        r = await api.get("/v1/site/tags")
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=60"
        assert r.json() == {"tags": [{**body["tags"][0], "shape": shape}, *body["tags"][1:]]}

        r = await api.get("/" + shape)
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/svg+xml"
        assert r.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert r.text.startswith('<svg xmlns="http://www.w3.org/2000/svg"') and "M12 2" in r.text
        stale = await api.get("/v1/site/tags/speedrun.svg?v=old")
        assert stale.status_code == 200 and stale.headers["cache-control"] == "public, max-age=60"
        for missing in ("new.svg", "speedrun", "speedrun.png", "Speedrun.svg", "..%2Fx.svg"):
            assert (await api.get(f"/v1/site/tags/{missing}")).status_code == 404, missing


async def test_older_list_reads_new_fields_as_null(app, api, clean):
    async with get_sessionmaker()() as s:  # as saved before the text and pattern fields
        s.add(SiteSetting(key="tags", value=[tag("old", color="red")] + AUTO, updated_by="api-key"))
        await s.commit()
    async with api:
        [old, *_] = (await api.get("/v1/site/tags")).json()["tags"]
    assert old == {**tag("old", color="red"), "shape": None, **dict.fromkeys(TEXT + PATTERN)}


async def test_shape_refusals(app, clean):
    async with client(app) as c:
        await c.put("/admin/site/tags", headers=KEY, json={"tags": AUTO})
        r = await c.put("/admin/site/tags/new/shape", headers={**KEY, "content-type": "text/xml"}, content=STAR)
        assert r.status_code == 415
        r = await c.put("/admin/site/tags/new/shape", headers=SVG, content=STAR[:-6] + b" " * 65536 + b"</svg>")
        assert r.status_code == 413
        r = await c.put("/admin/site/tags/new/shape", headers=SVG,
                        content=STAR.replace(b"<path", b"<script>alert(1)</script><path"))
        assert r.status_code == 400 and "<script>" in r.json()["msg"]
        r = await c.put("/admin/site/tags/nope/shape", headers=SVG, content=STAR)
        assert r.status_code == 404
        r = await c.put("/admin/site/tags/new/shape", headers={**SVG, "content-type": "image/svg+xml; charset=utf-8"},
                        content=STAR)
        assert r.status_code == 200
        assert (await c.delete("/admin/site/tags/nope/shape", headers=KEY)).status_code == 404


async def test_put_refused_changes_nothing(app, clean):
    async with client(app) as c:
        await c.put("/admin/site/tags", headers=KEY, json={"tags": AUTO})
        r = await c.put("/admin/site/tags", headers=KEY, json={"tags": AUTO[:2]})
        assert r.status_code == 400
        assert r.json() == {"error": True, "msg": r.json()["msg"], "tag": "compilation", "field": "name"}
        r = await c.put("/admin/site/tags", headers=KEY, json={"tags": [*AUTO, tag("x", color="url(http://e)")]})
        assert (r.status_code, r.json()["tag"], r.json()["field"]) == (400, "x", "color")
        assert len((await c.get("/admin/site/tags", headers=KEY)).json()["tags"]) == 3


async def test_unlisted_tag_loses_its_shape_and_audit(app, api, clean):
    async with client(app) as c:
        await c.put("/admin/site/tags", headers=KEY, json={"tags": [tag("speedrun"), *AUTO]})
        await c.put("/admin/site/tags/speedrun/shape", headers=SVG, content=STAR)
        await c.put("/admin/site/tags/new/shape", headers=SVG, content=STAR)
        r = await c.delete("/admin/site/tags/new/shape", headers=KEY)
        assert r.status_code == 200 and r.json()["tags"][1]["shape"] is None
        r = await c.put("/admin/site/tags", headers=KEY, json={"tags": AUTO})
        assert r.status_code == 200
        r = await c.put("/admin/site/tags", headers=KEY, json={"tags": [tag("speedrun"), *AUTO]})
        assert r.json()["tags"][0]["shape"] is None  # back in the list, but its old shape is gone
    async with api:
        assert (await api.get("/v1/site/tags/speedrun.svg")).status_code == 404

    rows = await _audit(clean)
    assert [(r.action, r.target) for r in rows] == [
        ("site.tags.replace", "site:tags"),
        ("site.tag.shape.set", "site-tag:speedrun"),
        ("site.tag.shape.set", "site-tag:new"),
        ("site.tag.shape.clear", "site-tag:new"),
        ("site.tags.replace", "site:tags"),
        ("site.tags.replace", "site:tags"),
    ]
    first, set_shape, _, cleared, dropped, _ = rows
    assert first.before is None and [t["name"] for t in first.after] == ["speedrun", "new", "updated",
                                                                         "compilation"]
    assert set_shape.before["shape"] is None and set_shape.after["shape"].startswith("v1/site/tags/speedrun.svg")
    assert cleared.before["shape"] and cleared.after["shape"] is None
    assert dropped.before[0]["shape"] and [t["name"] for t in dropped.after] == ["new", "updated", "compilation"]
    for row in rows:
        assert "<svg" not in str(row.before) + str(row.after) + str(row.detail)


async def test_session_needs_csrf(app, clean):
    async with client(app) as c:
        session = (await c.post("/admin/session", json={"password": "pw"})).json()
        assert (await c.get("/admin/site/tags")).status_code == 200
        assert (await c.put("/admin/site/tags", json={"tags": AUTO})).status_code == 403
        r = await c.put("/admin/site/tags", headers={"X-CSRF-Token": session["csrf"]}, json={"tags": AUTO})
        assert r.status_code == 200 and r.json()["updatedBy"] == "password"
        r = await c.put("/admin/site/tags/new/shape", headers={"content-type": "image/svg+xml"}, content=STAR)
        assert r.status_code == 403
        assert (await c.delete("/admin/site/tags/new/shape")).status_code == 403


# ── Tagging a VOD with the site's tags ────────────────────────────────────

TAGGED = "site-tags-test"


@pytest.fixture
async def vod(clean):
    async def drop():
        async with get_sessionmaker()() as s:
            await s.execute(delete(Vod).where(Vod.id == TAGGED))
            await s.commit()

    await drop()
    async with get_sessionmaker()() as s:
        s.add(Vod(id=TAGGED, title="t", created_at=dt.datetime.now(dt.timezone.utc), duration="01:00:00"))
        await s.commit()
    yield TAGGED
    await drop()


async def test_vod_takes_the_sites_tags(app, vod):
    async def patch(c, tags, v2=False):
        if v2:
            return await c.patch(f"/api/v2/vods/{vod}", headers=KEY, json={"tags": tags})
        return await c.patch(f"/admin/vods/{vod}", headers=KEY, json={"tags": tags})

    async with client(app) as c:
        # Never saved: only the built-in compilation.
        r = await patch(c, ["complete"])
        assert r.status_code == 400 and r.json()["msg"] == "unknown tag(s) complete; known: compilation"
        assert (await patch(c, ["compilation"])).status_code == 200

        await c.put("/admin/site/tags", headers=KEY, json={"tags": [tag("complete"), tag("speedrun"), *AUTO]})
        r = await patch(c, ["Complete", "compilation"])
        assert r.status_code == 200, r.text
        assert (await c.get(f"/admin/vods/{vod}", headers=KEY)).json()["tags"] == ["compilation", "complete"]
        r = await patch(c, ["speedrun"], v2=True)
        assert r.status_code == 200 and r.json()["tags"] == ["speedrun"]
        for computed in ("new", "updated"):  # the site works these out; a VOD never stores them
            r = await patch(c, [computed])
            assert r.status_code == 400 and "unknown tag(s)" in r.json()["msg"]
        r = await patch(c, ["nope"], v2=True)
        assert r.status_code == 422 and "known: compilation, complete, speedrun" in r.json()["detail"]

        # A tag the site's list drops stays allowed on the VOD that has it, and only there.
        await c.put("/admin/site/tags", headers=KEY, json={"tags": AUTO})
        assert (await patch(c, ["speedrun", "compilation"])).status_code == 200
        assert (await patch(c, ["complete"])).status_code == 400
