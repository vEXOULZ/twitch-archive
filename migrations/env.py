"""Alembic environment.

Uses ARCHIVE_MIGRATION_DATABASE_URL when set (migrations need DDL rights, the
app roles do not have them), otherwise ARCHIVE_DATABASE_URL.
"""

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from archive_common.config import get_settings
from archive_common.models import Base

target_metadata = Base.metadata


def _url() -> str:
    return os.environ.get("ARCHIVE_MIGRATION_DATABASE_URL") or get_settings().database_url


def run_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
