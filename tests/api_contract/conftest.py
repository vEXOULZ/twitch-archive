from __future__ import annotations

import httpx
import pytest


@pytest.fixture(scope="module")
async def client():
    from archive_common.config import Settings, get_settings

    get_settings.cache_clear()
    settings = get_settings()
    settings.rate_limit_points = 1_000_000
    try:
        import sqlalchemy
        from archive_common.db import get_engine

        async with get_engine().connect() as conn:
            await conn.execute(sqlalchemy.text("select 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")

    from archive_api.main import create_app

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    assert isinstance(settings, Settings)
