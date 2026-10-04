"""Changing how VOD tags show on the site (/admin/site/tags; archive_common/site_tags.py reads it).

PUT replaces the whole list, in order, all or nothing; the first refused tag and field is named.
A shape goes with its tag's name: a tag the list no longer has loses it.
"""

from __future__ import annotations

import re
from typing import Any

from archive_common import site_tags
from archive_common.db import get_sessionmaker
from archive_common.models import SiteSetting, SiteTagShape
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from .events import iso_utc
from .svg_clean import digest
from .vod_edits import KNOWN_TAGS

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")  # fullmatch: "$" would let a trailing newline through
LABEL_MAX = 40
SIZE = (8, 200)
TEXT_MAX = 24
TEXT_SIZE = (6, 48)
TEXT_NUDGE = (-100, 100)
TEXT_ROTATE = (-180, 180)
PATTERNS = ("stripes", "checks")
PATTERN_SIZE = (2, 40)
COMPUTED = ("new", "updated")  # the site works these out (from dates): never stored on a VOD

# The site's isTagColor (vexoulz-vods src/lib/vodTags.ts): it goes into CSS custom properties, so
# nothing that could load or run something (url(), image-set(), quotes, escapes, ";", ":", braces).
COLOR_MAX = 160
COLOR_FUNCS = {"rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color", "color-mix"}
MATH_FUNCS = {"calc", "min", "max", "clamp"}
COLOR_DEPTH = 4
_I = re.I | re.A  # re.A: [a-z] under re.I would also take "ſ" and the Kelvin sign
_COLOR_CHARS = re.compile(r"[#0-9a-z.,%\s/()*+-]+", _I)
_HEX_OR_NAME = re.compile(r"#[0-9a-f]{3,8}|[a-z]{3,20}", _I)
_TOKEN = re.compile(r"var\(--vx-[a-z0-9-]+\)", _I)
_LEFTOVER = re.compile(r"--|var\(", _I)
_OUTER = re.compile(r"([a-z-]+)\(", _I)
_BRACKET = re.compile(r"([a-z-]*)\(|\)", _I)


def is_color(value: Any) -> bool:
    """A hex, a color name, a theme token (``var(--vx-…)``), or one color function around the whole
    value, holding only theme tokens, color and math functions (at most 4 deep)."""
    if not isinstance(value, str) or len(value) > COLOR_MAX or not _COLOR_CHARS.fullmatch(value):
        return False
    if _HEX_OR_NAME.fullmatch(value):
        return True
    rest = _TOKEN.sub("v", value)  # theme tokens are the only var() and the only "--"
    if _LEFTOVER.search(rest):
        return False
    if rest == "v":
        return True
    outer = _OUTER.match(rest)
    if not outer or outer[1].lower() not in COLOR_FUNCS or not rest.endswith(")"):
        return False
    depth = 0
    for m in _BRACKET.finditer(rest):
        if m[0] == ")":
            depth -= 1
            if depth < 0 or (depth == 0 and m.end() != len(rest)):
                return False
        else:
            depth += 1
            if not m[1] or m[1].lower() not in COLOR_FUNCS | MATH_FUNCS or depth > COLOR_DEPTH:
                return False
    return depth == 0


class SiteTagError(Exception):
    def __init__(self, status: int, msg: str, tag: str | None = None, field: str | None = None) -> None:
        self.status, self.msg, self.tag, self.field = status, msg, tag, field


def _refuse(index: int, name: Any, field: str, msg: str) -> SiteTagError:
    named = name if isinstance(name, str) else None
    return SiteTagError(400, f"tags[{index}]{f' ({named})' if named else ''}.{field}: {msg}", named, field)


def _within(value: Any, bounds: tuple[int, int]) -> bool:
    return value is None or (type(value) is int and bounds[0] <= value <= bounds[1])


COLOR_RULE = "null or a color: a hex, a color name, var(--vx-…), or a color function (rgb() … oklch(), color-mix())"
NUMBERS = {
    "width": (SIZE, "px"),
    "height": (SIZE, "px"),
    "textSize": (TEXT_SIZE, "px"),
    "textX": (TEXT_NUDGE, "px"),
    "textY": (TEXT_NUDGE, "px"),
    "textRotate": (TEXT_ROTATE, "degrees"),
    "patternSize": (PATTERN_SIZE, "px"),
}


def _fields(i: int, name: str, tag: dict[str, Any]) -> dict[str, Any]:
    """The tag's stored fields past ``drawn``, checked; the text and pattern fields null without
    text or a pattern."""
    out: dict[str, Any] = {}
    for field in ("color", "textColor", "patternColor"):
        if tag.get(field) is not None and not is_color(tag[field]):
            raise _refuse(i, name, field, COLOR_RULE)
        out[field] = tag.get(field)
    for field, (bounds, unit) in NUMBERS.items():
        if not _within(tag.get(field), bounds):
            raise _refuse(i, name, field, f"null or a whole number of {unit}, {bounds[0]} to {bounds[1]}")
        out[field] = tag.get(field)
    text = tag.get("text")
    if text is not None:
        text = text.strip() if isinstance(text, str) else ""
        if not 1 <= len(text) <= TEXT_MAX:
            raise _refuse(i, name, "text", f"null or 1-{TEXT_MAX} characters")
    pattern = tag.get("pattern")
    if pattern is not None and pattern not in PATTERNS:
        raise _refuse(i, name, "pattern", f"null, {' or '.join(map(repr, PATTERNS))}")
    out.update(text=text, pattern=pattern)
    for gate, fields in (("text", site_tags.TEXT_FIELDS), ("pattern", site_tags.PATTERN_FIELDS)):
        if out[gate] is None:
            out.update(dict.fromkeys(fields))
    return out


def validate(body: Any) -> list[dict[str, Any]]:
    """The tags to store (``site_tags.FIELDS`` each), or ``SiteTagError`` for the first refused one."""
    tags = body.get("tags") if isinstance(body, dict) else None
    if not isinstance(tags, list):
        raise SiteTagError(400, "tags must be a list", field="tags")
    if len(tags) > site_tags.MAX_TAGS:
        raise SiteTagError(400, f"at most {site_tags.MAX_TAGS} tags", field="tags")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, tag in enumerate(tags):
        if not isinstance(tag, dict):
            raise SiteTagError(400, f"tags[{i}] must be an object", field="tags")
        name = tag.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise _refuse(i, name, "name", "lowercase letters, digits and dashes, 1-32, not starting with a dash")
        if name in seen:
            raise _refuse(i, name, "name", "listed twice")
        seen.add(name)
        label = tag.get("label")
        if not isinstance(label, str) or not 1 <= len(label) <= LABEL_MAX:
            raise _refuse(i, name, "label", f"1-{LABEL_MAX} characters")
        if not isinstance(tag.get("drawn"), bool):
            raise _refuse(i, name, "drawn", "must be true or false")
        checked = {"name": name, "label": label, "drawn": tag["drawn"], **_fields(i, name, tag)}
        out.append({k: checked[k] for k in site_tags.FIELDS})
    for auto in site_tags.AUTO_TAGS:
        if auto not in seen:
            raise SiteTagError(400, f"the {auto!r} tag is set automatically and must stay in the list", auto, "name")
    return out


def view(loaded: dict[str, Any] | None) -> dict[str, Any]:
    """GET /admin/site/tags: ``site_tags.load``'s, or an empty list while never saved."""
    if loaded is None:
        return {"tags": [], "updatedAt": None, "updatedBy": None}
    return {**loaded, "updatedAt": iso_utc(loaded["updatedAt"])}


async def _locked(s) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    """The saved list, its row locked until the change commits (one change at a time)."""
    await s.execute(select(SiteSetting.key).where(SiteSetting.key == site_tags.KEY).with_for_update())
    return await site_tags.load(s)


def _tags(loaded: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    return loaded["tags"] if loaded else None


async def save(tags: list[dict[str, Any]], updated_by: str) -> tuple[Any, Any, dict[str, Any]]:
    """Replace the list. (before, after) tag lists for the audit, and the new view."""
    async with get_sessionmaker()() as s:
        before = await _locked(s)
        stmt = insert(SiteSetting).values(key=site_tags.KEY, value=tags, updated_by=updated_by)
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=[SiteSetting.key],
                set_={"value": stmt.excluded.value, "updated_by": stmt.excluded.updated_by, "updated_at": func.now()},
            )
        )
        await s.execute(delete(SiteTagShape).where(SiteTagShape.name.not_in([t["name"] for t in tags])))
        after = await site_tags.load(s)
        await s.commit()
    return _tags(before), _tags(after), view(after)


def _tag(loaded: dict[str, Any] | None, name: str) -> dict[str, Any]:
    for tag in _tags(loaded) or []:
        if tag["name"] == name:
            return tag
    raise SiteTagError(404, f"No saved tag {name}")


async def set_shape(name: str, svg: str | None) -> tuple[Any, Any, dict[str, Any]]:
    """The tag's shape (a cleaned SVG), or none with ``svg=None``. (before, after) of the tag, and the view."""
    async with get_sessionmaker()() as s:
        loaded = await _locked(s)
        before = _tag(loaded, name)
        if svg is None:
            await s.execute(delete(SiteTagShape).where(SiteTagShape.name == name))
        else:
            stmt = insert(SiteTagShape).values(name=name, svg=svg, hash=digest(svg))
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[SiteTagShape.name],
                    set_={"svg": stmt.excluded.svg, "hash": stmt.excluded.hash, "updated_at": func.now()},
                )
            )
        after = await site_tags.load(s)
        await s.commit()
    return before, _tag(after, name), view(after)


async def vod_tags(s, keep: Any = ()) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
    """The tags a VOD can be given: the site's list, less the ones it works out itself (``new``,
    ``updated``), or ``vod_edits.KNOWN_TAGS`` while the list was never saved. ``keep``: the VOD's own
    tags, which stay allowed after the site's list drops them."""
    loaded = await site_tags.load(s)
    names = KNOWN_TAGS if loaded is None else [t["name"] for t in loaded["tags"] if t["name"] not in COMPUTED]
    return tuple(sorted({*names, *(keep or ())}))
