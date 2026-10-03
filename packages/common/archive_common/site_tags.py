"""How each VOD tag shows on the site (vods.vexoulz.net's /manage/tags), as both services read it.

The worker's admin routes write it (archive_worker/site_tags.py), archive-api serves it at
``/v1/site/tags``. A tag is ``{name, label, drawn, color, shape, width, height}``, then the text drawn
on it (``TEXT_FIELDS``) and its pattern (``PATTERN_FIELDS``); ``shape`` is not stored with the list
but made from ``site_tag_shapes``: the path of the tag's SVG relative to the public API, versioned by
its content's hash. A list saved before a field existed reads it as null.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .models import SiteSetting, SiteTagShape

KEY = "tags"  # the site_settings row
AUTO_TAGS = ("new", "updated", "compilation")  # set by the site or the archive: every list keeps them
MAX_TAGS = 32
SHAPE_MAX_BYTES = 64 * 1024
TEXT_FIELDS = ("text", "textColor", "textSize", "textX", "textY", "textRotate")  # all null without text
PATTERN_FIELDS = ("pattern", "patternColor", "patternSize")  # all null without a pattern
FIELDS = ("name", "label", "drawn", "color", "width", "height", *TEXT_FIELDS, *PATTERN_FIELDS)  # stored, in this order


def shape_path(name: str, digest: str) -> str:
    return f"v1/site/tags/{name}.svg?v={digest}"


def with_shapes(tags: list[dict[str, Any]], hashes: dict[str, str]) -> list[dict[str, Any]]:
    """The stored tags as the API shows them: ``shape`` after ``color``, as the contract lists it."""
    out = []
    for tag in tags:
        digest = hashes.get(tag["name"])
        shown = {k: tag.get(k) for k in ("name", "label", "drawn", "color")}
        shown["shape"] = shape_path(tag["name"], digest) if digest else None
        shown.update({k: tag.get(k) for k in FIELDS[4:]})
        out.append(shown)
    return out


async def load(conn: Any) -> dict[str, Any] | None:
    """``{"tags", "updatedAt", "updatedBy"}`` (``updatedAt`` a datetime), or None while the list was
    never saved. ``conn``: an async connection or session."""
    row = (
        await conn.execute(
            select(SiteSetting.value, SiteSetting.updated_at, SiteSetting.updated_by).where(SiteSetting.key == KEY)
        )
    ).first()
    if row is None:
        return None
    hashes = dict((await conn.execute(select(SiteTagShape.name, SiteTagShape.hash))).all())
    return {
        "tags": with_shapes(list(row.value or []), hashes),
        "updatedAt": row.updated_at,
        "updatedBy": row.updated_by,
    }


async def shape(conn: Any, name: str) -> tuple[str, str] | None:
    """A tag's cleaned SVG and its hash, or None if it has no shape."""
    row = (await conn.execute(select(SiteTagShape.svg, SiteTagShape.hash).where(SiteTagShape.name == name))).first()
    return (row.svg, row.hash) if row else None
