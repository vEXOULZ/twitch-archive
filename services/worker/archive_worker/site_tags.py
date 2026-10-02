"""Changing how VOD tags show on the site (/admin/site/tags; archive_common/site_tags.py reads it).

PUT replaces the whole list, in order, all or nothing; the first refused tag and field is named.
A shape goes with its tag's name: a tag the list no longer has loses it.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from archive_common import site_tags
from archive_common.db import get_sessionmaker
from archive_common.models import SiteSetting, SiteTagShape

from .events import iso_utc
from .svg_clean import digest
from .vod_edits import KNOWN_TAGS

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
LABEL_MAX = 40
SIZE_MIN, SIZE_MAX = 8, 200
COMPUTED = ("new", "updated")  # the site works these out (from dates): never stored on a VOD
COLOR = re.compile(
    r"^(#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})"
    r"|var\(--vx-[a-z0-9-]+\)"
    r"|[a-zA-Z]{3,20}"
    r"|(?:rgba?|hsla?|oklch)\([0-9a-zA-Z.,%/ ]{1,60}\))$"
)


class SiteTagError(Exception):
    def __init__(self, status: int, msg: str, tag: str | None = None, field: str | None = None) -> None:
        self.status, self.msg, self.tag, self.field = status, msg, tag, field


def _refuse(index: int, name: Any, field: str, msg: str) -> SiteTagError:
    named = name if isinstance(name, str) else None
    return SiteTagError(400, f"tags[{index}]{f' ({named})' if named else ''}.{field}: {msg}", named, field)


def _size(value: Any) -> bool:
    return value is None or (type(value) is int and SIZE_MIN <= value <= SIZE_MAX)


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
        if not isinstance(name, str) or not NAME.match(name):
            raise _refuse(i, name, "name", "lowercase letters, digits and dashes, 1-32, not starting with a dash")
        if name in seen:
            raise _refuse(i, name, "name", "listed twice")
        seen.add(name)
        label = tag.get("label")
        if not isinstance(label, str) or not 1 <= len(label) <= LABEL_MAX:
            raise _refuse(i, name, "label", f"1-{LABEL_MAX} characters")
        if not isinstance(tag.get("drawn"), bool):
            raise _refuse(i, name, "drawn", "must be true or false")
        color = tag.get("color")
        if color is not None and not (isinstance(color, str) and COLOR.match(color)):
            raise _refuse(i, name, "color", "null, a hex color, var(--vx-…), a color name, or rgb()/hsl()/oklch()")
        for field in ("width", "height"):
            if not _size(tag.get(field)):
                raise _refuse(i, name, field, f"null or a whole number of px, {SIZE_MIN}-{SIZE_MAX}")
        out.append({k: tag.get(k) for k in site_tags.FIELDS})
    for auto in site_tags.AUTO_TAGS:
        if auto not in seen:
            raise SiteTagError(400, f"the {auto!r} tag is set automatically and must stay in the list", auto, "name")
    return out


def view(loaded: dict[str, Any] | None) -> dict[str, Any]:
    """GET /admin/site/tags: ``site_tags.load``'s, or an empty list while never saved."""
    if loaded is None:
        return {"tags": [], "updatedAt": None, "updatedBy": None}
    return {**loaded, "updatedAt": iso_utc(loaded["updatedAt"])}


async def _locked(s) -> dict[str, Any] | None:
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
        await s.execute(stmt.on_conflict_do_update(
            index_elements=[SiteSetting.key],
            set_={"value": stmt.excluded.value, "updated_by": stmt.excluded.updated_by, "updated_at": func.now()}))
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
            await s.execute(stmt.on_conflict_do_update(
                index_elements=[SiteTagShape.name],
                set_={"svg": stmt.excluded.svg, "hash": stmt.excluded.hash, "updated_at": func.now()}))
        after = await site_tags.load(s)
        await s.commit()
    return before, _tag(after, name), view(after)


async def vod_tags(s, keep: Any = ()) -> tuple[str, ...]:
    """The tags a VOD can be given: the site's list, less the ones it works out itself (``new``,
    ``updated``), or ``vod_edits.KNOWN_TAGS`` while the list was never saved. ``keep``: the VOD's own
    tags, which stay allowed after the site's list drops them."""
    loaded = await site_tags.load(s)
    names = KNOWN_TAGS if loaded is None else [t["name"] for t in loaded["tags"] if t["name"] not in COMPUTED]
    return tuple(sorted({*names, *(keep or ())}))
