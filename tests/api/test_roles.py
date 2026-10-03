"""deploy/roles.sql must grant the service roles every table they use: a table added by a migration
but not granted there makes each query that reads it fail with permission denied in production
(tests run as a superuser, so nothing else catches it)."""

import importlib
import pkgutil
import re
from pathlib import Path

import archive_api
from archive_common import serialize
from archive_common.models import Base
from sqlalchemy import Table

ROLES_SQL = Path(__file__).parents[2] / "deploy" / "roles.sql"
# GRANT <privileges> ON <tables> TO <roles>; (not ON ALL ... / SCHEMA / DATABASE)
GRANT = re.compile(r"\bGRANT\s+[\w\s,]+?\s+ON\s+(?!ALL\b|SCHEMA\b|DATABASE\b)([\w\s,]+?)\s+TO\s+([\w\s,]+?);", re.I)


def granted(role: str) -> set[str]:
    sql = re.sub(r"--[^\n]*", "", ROLES_SQL.read_text(encoding="utf-8"))
    out: set[str] = set()
    for tables, roles in GRANT.findall(sql):
        if role in {r.strip() for r in roles.split(",")}:
            out |= {t.strip() for t in tables.split(",")}
    return out


def _tables_in(module) -> set[str]:
    out = set()
    for value in vars(module).values():
        table = value if isinstance(value, Table) else getattr(value, "__table__", None)
        if isinstance(table, Table) and table.schema is None and table.name in Base.metadata.tables:
            out.add(table.name)
    return out


def test_worker_is_granted_every_table():
    missing = set(Base.metadata.tables) - granted("archive_worker")
    assert not missing, f"deploy/roles.sql grants archive_worker nothing on {sorted(missing)}"


def test_api_is_granted_the_tables_it_reads():
    modules = [serialize] + [
        importlib.import_module(m.name) for m in pkgutil.walk_packages(archive_api.__path__, "archive_api.")
    ]
    used = set().union(*map(_tables_in, modules))
    assert "vod_segments" in used  # (the check sees serialize's tables)
    missing = used - granted("archive_api")
    assert not missing, f"deploy/roles.sql grants archive_api nothing on {sorted(missing)}"
