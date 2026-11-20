"""Fixtures for integration tests against a real PostgreSQL + PostGIS database.

Set ``SSG_TEST_DATABASE_URL`` (e.g. ``postgresql+psycopg://postgres@localhost/signalserver_test``)
to run them; they are skipped otherwise. The database is wiped.
"""

import io
import os
import stat
import sys
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pytest

TEST_DB = os.environ.get("SSG_TEST_DATABASE_URL")
HERE = Path(__file__).parent


def _hgt_bytes(base: int) -> bytes:
    grid = (np.arange(1201 * 1201, dtype=np.int32).reshape(1201, 1201) % 500 + base).astype(">i2")
    return grid.tobytes()


def fake_viewfinder() -> tuple[httpx.MockTransport, list[str]]:
    """Serve L32.zip with two land tiles; record requested URLs."""
    calls: list[str] = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("L32/N47E009.hgt", _hgt_bytes(400))
        zf.writestr("L32/N46E009.hgt", _hgt_bytes(1000))
    payload = buf.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url).endswith("/L32.zip"):
            return httpx.Response(200, content=payload)
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


@pytest.fixture(scope="session")
def settings(tmp_path_factory):
    if not TEST_DB:
        pytest.skip("SSG_TEST_DATABASE_URL not set")
    data = tmp_path_factory.mktemp("data")
    engine = HERE / "fake_signalserver.py"
    engine.chmod(engine.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    os.environ.update(
        SSG_DATABASE_URL=TEST_DB,
        SSG_DATA_DIR=str(data),
        SSG_SIGNALSERVER_PATH=str(engine),
        SSG_FRONTEND_DIST=str(data / "no-frontend"),
    )
    from signalserver_gui import config, db

    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    s = config.get_settings()
    assert s.database_url == TEST_DB

    from sqlalchemy import create_engine, text

    eng = create_engine(TEST_DB)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    eng.dispose()

    from signalserver_gui.cli import migrate, seed

    migrate(TEST_DB)
    seed()
    return s


@pytest.fixture(scope="session")
def viewfinder(settings):
    transport, calls = fake_viewfinder()
    from signalserver_gui.elevation import provider

    original = provider.ElevationProvider._http
    provider.ElevationProvider._http = lambda self: httpx.Client(transport=transport)
    yield calls
    provider.ElevationProvider._http = original


@pytest.fixture(scope="session")
def client(settings, viewfinder):
    from fastapi.testclient import TestClient

    from signalserver_gui.api.app import create_app

    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def worker(settings):
    from signalserver_gui.worker import Worker

    return Worker(settings)


sys.path.insert(0, str(HERE.parent))
