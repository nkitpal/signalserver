"""Fixtures for integration tests against a real PostgreSQL + PostGIS database.

Set ``SSG_TEST_DATABASE_URL`` (e.g. ``postgresql+psycopg://postgres@localhost/signalserver_test``)
to run them; they are skipped otherwise. The database is wiped.

All external services (Viewfinder, Copernicus, Overpass, Protomaps) are served
by an in-memory mock, so no network is needed.
"""

import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pytest

TEST_DB = os.environ.get("SSG_TEST_DATABASE_URL")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))


def _hgt_bytes(base: int) -> bytes:
    grid = (np.arange(1201 * 1201, dtype=np.int32).reshape(1201, 1201) % 500 + base).astype(">i2")
    return grid.tobytes()


def make_cop_tif(base: float = 500.0) -> bytes:
    """A GLO-30-like COG for N47E009: 3600x3600 Float32, pixel-is-point, NW corner at (9, 48)."""
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin

    res = 1 / 3600
    data = np.full((3600, 3600), base, dtype=np.float32)
    data[:, 1800:] += 200  # eastern half higher
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", width=3600, height=3600, count=1, dtype="float32",
                      crs="EPSG:4326", transform=from_origin(9 - res / 2, 48 + res / 2, res, res),
                      compress="deflate") as dst:
            dst.write(data, 1)
            dst.update_tags(AREA_OR_POINT="Point")
        return mem.read()


def make_pmtiles() -> bytes:
    """A tiny vector-tile PMTiles archive (z0-6, all tiles) for basemap extraction tests."""
    from pmtiles.tile import Compression, TileType, zxy_to_tileid
    from pmtiles.writer import Writer

    buf = io.BytesIO()
    w = Writer(buf)
    ids = []
    for z in range(0, 7):
        n = 2**z
        ids += [(zxy_to_tileid(z, x, y), z, x, y) for x in range(n) for y in range(n)]
    for tid, z, x, y in sorted(ids):
        w.write_tile(tid, f"tile-{z}-{x}-{y}".encode())
    w.finalize({"tile_type": TileType.MVT, "tile_compression": Compression.NONE},
               {"vector_layers": [], "attribution": "test"})
    return buf.getvalue()


OVERPASS_RESPONSE = {
    "elements": [
        {"type": "way", "tags": {"building": "yes", "height": "30 m"},
         "geometry": [{"lat": 47.3, "lon": 9.3}, {"lat": 47.3, "lon": 9.301}, {"lat": 47.301, "lon": 9.301},
                      {"lat": 47.301, "lon": 9.3}, {"lat": 47.3, "lon": 9.3}]},
        {"type": "way", "tags": {"building": "house", "building:levels": "2"},
         "geometry": [{"lat": 47.31, "lon": 9.31}, {"lat": 47.31, "lon": 9.3101}, {"lat": 47.3101, "lon": 9.3101},
                      {"lat": 47.31, "lon": 9.31}]},
    ]
}


class MockServices:
    def __init__(self):
        self.calls: list[str] = []
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("L32/N47E009.hgt", _hgt_bytes(400))
            zf.writestr("L32/N46E009.hgt", _hgt_bytes(1000))
        self.l32 = buf.getvalue()
        self.cop = make_cop_tif()
        self.pmtiles = make_pmtiles()
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (20, 120, 40)).save(buf, "JPEG")
        self.jpeg = buf.getvalue()

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if url.endswith("/L32.zip"):
            return httpx.Response(200, content=self.l32)
        if "eox" in url:  # satellite tiles: a tiny JPEG for every request
            return httpx.Response(200, content=self.jpeg, headers={"content-type": "image/jpeg"})
        if "copernicus" in url:
            if "N47_00_E009_00" in url:
                return httpx.Response(200, content=self.cop)
            return httpx.Response(404)
        if "overpass" in url:
            return httpx.Response(200, json=OVERPASS_RESPONSE)
        if url.endswith("builds.json"):
            return httpx.Response(200, json=[{"key": "20260101.pmtiles"}, {"key": "20260102.pmtiles"}])
        if url.endswith(".pmtiles"):
            rng = request.headers.get("range", "")
            m = re.match(r"bytes=(\d+)-(\d+)", rng)
            if not m:
                return httpx.Response(200, content=self.pmtiles)
            a, b = int(m.group(1)), int(m.group(2))
            return httpx.Response(206, content=self.pmtiles[a : b + 1])
        return httpx.Response(404)

    def client(self, *args, **kwargs) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture(scope="session")
def settings(tmp_path_factory):
    if not TEST_DB:
        pytest.skip("SSG_TEST_DATABASE_URL not set")
    data = tmp_path_factory.mktemp("data")
    bindir = tmp_path_factory.mktemp("bin")
    fake = HERE / "fake_signalserver.py"
    for name in ("signalserver", "signalserverHD"):
        # Wrapper per binary name; the fake engine reads HD mode from SSG_FAKE_BINARY.
        script = bindir / name
        script.write_text(f'#!/bin/sh\nSSG_FAKE_BINARY={name} exec "{sys.executable}" "{fake}" "$@"\n')
        script.chmod(0o755)
    os.environ.update(
        SSG_DATABASE_URL=TEST_DB,
        SSG_DATA_DIR=str(data),
        SSG_SIGNALSERVER_PATH=str(bindir / "signalserver"),
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
def services(settings):
    mock = MockServices()
    from signalserver_gui import basemap
    from signalserver_gui.elevation import buildings, provider

    originals = (provider.ElevationProvider._http, buildings.BuildingSource._http, basemap.httpx.Client)
    provider.ElevationProvider._http = lambda self: mock.client()
    buildings.BuildingSource._http = lambda self: mock.client()

    class _Httpx:  # only basemap's module-level httpx.Client is swapped
        Client = staticmethod(mock.client)
        HTTPError = httpx.HTTPError

    basemap.httpx = _Httpx
    yield mock
    provider.ElevationProvider._http, buildings.BuildingSource._http = originals[:2]
    basemap.httpx = httpx


@pytest.fixture(scope="session")
def viewfinder(services):
    return services.calls


@pytest.fixture(scope="session")
def client(settings, services):
    from fastapi.testclient import TestClient

    from signalserver_gui.api.app import create_app

    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def worker(settings):
    from signalserver_gui.worker import Worker

    return Worker(settings)


def json_body(resp):
    return json.loads(resp.content)
