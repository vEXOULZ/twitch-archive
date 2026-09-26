"""Translate Feathers REST query strings into SQLAlchemy clauses.

The frontend uses the Feathers REST client, which serialises queries with
``qs`` bracket notation, e.g. ``?createdAt[$gte]=...&$sort[createdAt]=-1``.
Supported: equality, ``$ne $lt $lte $gt $gte $in $nin $like $notLike $iLike
$notILike $or $and``, plus ``$limit $skip $sort $select``. Only whitelisted
attributes are queryable; anything else is a 400.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

from sqlalchemy import Boolean, ColumnElement, Text, and_, cast, func, literal, or_, true
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH

from archive_common.serialize import Resource

from .errors import FeathersError

_KEY_RE = re.compile(r"^([^\[\]]+)((?:\[[^\[\]]*\])*)$")


# ── qs bracket parsing ────────────────────────────────────────────────────


def parse_query_string(qs: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for raw_key, value in parse_qsl(qs, keep_blank_values=True):
        m = _KEY_RE.match(raw_key)
        if not m:
            root[raw_key] = value
            continue
        parts = [m.group(1)] + re.findall(r"\[([^\[\]]*)\]", m.group(2))
        node: Any = root
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            nxt = None if last else parts[i + 1]
            if isinstance(node, list):
                # "a[]" append semantics
                if last:
                    node.append(value)
                else:
                    child: Any = [] if nxt == "" else {}
                    node.append(child)
                    node = child
                continue
            if last:
                if part in node:
                    existing = node[part]
                    node[part] = existing + [value] if isinstance(existing, list) else [existing, value]
                else:
                    node[part] = value
            else:
                if part not in node or not isinstance(node[part], (dict, list)):
                    node[part] = [] if nxt == "" else {}
                node = node[part]
    return _listify(root)


def _listify(node: Any) -> Any:
    """qs turns {"0": a, "1": b} into [a, b]."""
    if isinstance(node, dict):
        node = {k: _listify(v) for k, v in node.items()}
        if node and all(k.isdigit() for k in node):
            return [node[k] for k in sorted(node, key=int)]
        return node
    if isinstance(node, list):
        return [_listify(v) for v in node]
    return node


# ── Query building ────────────────────────────────────────────────────────

Special = Callable[[Any], ColumnElement[bool]]


@dataclass
class ParsedQuery:
    where: ColumnElement[bool]
    order_by: list
    limit: int
    skip: int
    select: set[str] | None
    query: dict[str, Any]  # the parsed query string, for filters of a service's own


def _as_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return list(v.values())
    return [v]


def typed_value(col, v: Any):
    """``v`` (a query-string value) as a literal of ``col``'s type; Postgres does the cast."""
    if v is None:
        return None
    if not isinstance(v, str):
        raise FeathersError(400, f"Invalid value for '{col.name}'")
    if isinstance(col.type, Text):
        return literal(v, Text)
    return cast(literal(v, Text), col.type)


_OPS: dict[str, Callable] = {
    "$ne": lambda c, v: c.is_not(None) if v is None else c != typed_value(c, v),
    "$lt": lambda c, v: c < typed_value(c, v),
    "$lte": lambda c, v: c <= typed_value(c, v),
    "$gt": lambda c, v: c > typed_value(c, v),
    "$gte": lambda c, v: c >= typed_value(c, v),
    "$in": lambda c, v: c.in_([typed_value(c, x) for x in _as_list(v)]),
    "$nin": lambda c, v: c.not_in([typed_value(c, x) for x in _as_list(v)]),
    "$like": lambda c, v: c.like(v),
    "$notLike": lambda c, v: c.not_like(v),
    "$iLike": lambda c, v: c.ilike(v),
    "$notILike": lambda c, v: c.not_ilike(v),
}


def build_where(resource: Resource, query: dict[str, Any], special: dict[str, Special]) -> ColumnElement[bool]:
    clauses: list[ColumnElement[bool]] = []
    for key, value in query.items():
        if key in ("$or", "$and"):
            subs = [build_where(resource, q, special) for q in _as_list(value) if isinstance(q, dict)]
            if subs:
                clauses.append(or_(*subs) if key == "$or" else and_(*subs))
            continue
        if key.startswith("$"):
            continue
        if key in special:
            clauses.append(special[key](value))
            continue
        field = resource.field(key)
        if field is None or isinstance(field.column.type, JSONB):
            raise FeathersError(400, f"Invalid query parameter '{key}'")
        col = field.column
        if isinstance(value, dict):
            for op, operand in value.items():
                fn = _OPS.get(op)
                if fn is None:
                    raise FeathersError(400, f"Invalid query parameter '{key}[{op}]'")
                if op in ("$like", "$notLike", "$iLike", "$notILike") and not isinstance(operand, str):
                    raise FeathersError(400, f"Invalid value for '{key}[{op}]'")
                clauses.append(fn(col, operand))
        elif isinstance(value, list):
            clauses.append(col.in_([typed_value(col, x) for x in value]))
        else:
            clauses.append(col == typed_value(col, value))
    if not clauses:
        return true()
    return and_(*clauses)


def _int(v: Any, name: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise FeathersError(400, f"Invalid {name}") from None


def parse(
    resource: Resource,
    qs: str,
    *,
    default_limit: int,
    max_limit: int,
    special: dict[str, Special] | None = None,
) -> ParsedQuery:
    query = parse_query_string(qs)
    where = build_where(resource, query, special or {})

    limit = default_limit
    if "$limit" in query:
        limit = _int(query["$limit"], "$limit")
        if limit < 0:
            limit = default_limit
    limit = min(limit, max_limit)
    skip = max(0, _int(query.get("$skip", 0), "$skip"))

    order_by = []
    sort = query.get("$sort")
    if isinstance(sort, dict):
        for key, direction in sort.items():
            field = resource.field(key)
            if field is None:
                raise FeathersError(400, f"Invalid $sort attribute '{key}'")
            order_by.append(field.column.desc() if str(direction).strip() == "-1" else field.column.asc())

    select = None
    if "$select" in query:
        keys = {resource.aliases.get(k, k) for k in _as_list(query["$select"])}
        unknown = keys - set(resource.by_key)
        if unknown:
            raise FeathersError(400, f"Invalid $select attribute(s): {', '.join(sorted(unknown))}")
        select = keys | {resource.id_key}

    return ParsedQuery(where=where, order_by=order_by, limit=limit, skip=skip, select=select, query=query)


# ── Chapter filters (vods only) ────────────────────────────────────────────

_PG_REGEX_SPECIAL = re.compile(r"([\\.^$|?*+()\[\]{}])")


def chapter_filter(chapters_col) -> Special:
    """``chapters[...]`` filters; each one matches when *any* chapter matches.

    * ``chapters[name]=x``: case-insensitive substring of the name (legacy)
    * ``chapters[name][$eq]=x``: exact, case-sensitive name
    * ``chapters[gameId]=id``: exact gameId
    * ``chapters[gameId]=null``: a chapter with no Twitch category (gameId null)

    Several keys combine with AND. User input is never interpolated into the
    JSONPath: the substring is regex-escaped (as the legacy API should have
    done), the exact matches are passed as JSONPath variables.
    """
    def jsonpath(path: str) -> ColumnElement:
        return cast(literal(path, Text), JSONPATH)

    def exists(path: str, value: str) -> ColumnElement[bool]:
        return func.jsonb_path_exists(chapters_col, jsonpath(path), literal({"v": value}, JSONB), type_=Boolean)

    def invalid(key: str = "") -> FeathersError:
        return FeathersError(400, f"Invalid query parameter 'chapters{key}'")

    def name(value: Any) -> ColumnElement[bool]:
        if isinstance(value, str):
            pattern = ".*" + _PG_REGEX_SPECIAL.sub(r"\\\1", value) + ".*"
            path = f"$[*] ? (@.name like_regex {json.dumps(pattern, ensure_ascii=False)} flag \"i\")"
            return chapters_col.op("@?")(jsonpath(path))
        if isinstance(value, dict) and set(value) == {"$eq"} and isinstance(value["$eq"], str):
            return exists("$[*] ? (@.name == $v)", value["$eq"])
        raise invalid("[name]")

    def game_id(value: Any) -> ColumnElement[bool]:
        if not isinstance(value, str) or not value:
            raise invalid("[gameId]")
        if value == "null":
            return chapters_col.op("@?")(jsonpath("$[*] ? (@.gameId == null)"))
        return exists("$[*] ? (@.gameId == $v)", value)

    builders = {"name": name, "gameId": game_id}

    def build(value: Any) -> ColumnElement[bool]:
        if not isinstance(value, dict) or not value:
            raise invalid()
        clauses = []
        for key, operand in value.items():
            if key not in builders:
                raise invalid(f"[{key}]")
            clauses.append(builders[key](operand))
        return and_(*clauses)

    return build
