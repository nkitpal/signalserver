"""Offline street-map packs: region extracts of the Protomaps OpenStreetMap basemap.

Protomaps publishes a daily planet build as one PMTiles archive (vector tiles,
ODbL OpenStreetMap data) that may be extracted for offline use. We read only
the parts we need with HTTP range requests:

1. header + root directory, then the leaf directories covering our tile IDs,
2. the tile data, fetched in merged byte ranges,

and write a new, self-contained PMTiles file per pack. The browser renders the
vector tiles itself (protomaps-leaflet), so no image tiles are stored.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from collections.abc import Callable

import httpx
from pmtiles.tile import (
    Compression,
    TileType,
    deserialize_directory,
    deserialize_header,
    find_tile,
    zxy_to_tileid,
)
from pmtiles.writer import Writer
from sqlalchemy.orm import Session

from .config import Settings
from .models import BasemapPack

log = logging.getLogger(__name__)

WORLD_ZOOM = 5  # zooms 0..WORLD_ZOOM-1 are included for the whole world (context when zoomed out)
MERGE_GAP = 64 * 1024
MAX_BATCH = 8 * 1024 * 1024


class BasemapError(RuntimeError):
    pass


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    lat = max(min(lat, 85.0511), -85.0511)
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_ids_for(west: float, south: float, east: float, north: float, max_zoom: int) -> list[int]:
    ids: set[int] = set()
    for z in range(0, max_zoom + 1):
        if z < WORLD_ZOOM:
            n = 2**z
            ids.update(zxy_to_tileid(z, x, y) for x in range(n) for y in range(n))
            continue
        x0, y0 = lonlat_to_tile(west, north, z)
        x1, y1 = lonlat_to_tile(east, south, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                ids.add(zxy_to_tileid(z, x, y))
    return sorted(ids)


def count_tiles(west: float, south: float, east: float, north: float, max_zoom: int) -> int:
    total = 0
    for z in range(0, max_zoom + 1):
        if z < WORLD_ZOOM:
            total += 4**z
            continue
        x0, y0 = lonlat_to_tile(west, north, z)
        x1, y1 = lonlat_to_tile(east, south, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


class RemoteArchive:
    """Read-only PMTiles v3 archive over HTTP range requests."""

    def __init__(self, url: str, client: httpx.Client):
        self.url = url
        self.client = client
        self.header = deserialize_header(self.get(0, 127))
        if self.header["internal_compression"] != Compression.GZIP:
            raise BasemapError("Only gzip-compressed PMTiles directories are supported")
        self.root = deserialize_directory(self.get(self.header["root_offset"], self.header["root_length"]))
        self._leaves: dict[int, list] = {}

    def get(self, offset: int, length: int) -> bytes:
        resp = self.client.get(self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
        if resp.status_code not in (200, 206):
            raise BasemapError(f"Range request to {self.url} failed: HTTP {resp.status_code}")
        data = resp.content
        if resp.status_code == 200:  # server ignored the range
            data = data[offset : offset + length]
        if len(data) != length:
            raise BasemapError("Short read from basemap source")
        return data

    def metadata(self) -> dict:
        import gzip
        import json

        raw = self.get(self.header["metadata_offset"], self.header["metadata_length"])
        if self.header["internal_compression"] == Compression.GZIP:
            raw = gzip.decompress(raw)
        return json.loads(raw)

    def locate(self, tile_id: int) -> tuple[int, int] | None:
        """Absolute (offset, length) of a tile's data, or None if absent."""
        entries = self.root
        for _ in range(4):
            entry = find_tile(entries, tile_id)
            if entry is None:
                return None
            if entry.run_length > 0:
                return self.header["tile_data_offset"] + entry.offset, entry.length
            leaf_off = self.header["leaf_directory_offset"] + entry.offset
            if leaf_off not in self._leaves:
                self._leaves[leaf_off] = deserialize_directory(self.get(leaf_off, entry.length))
            entries = self._leaves[leaf_off]
        raise BasemapError("PMTiles directory too deep")


def latest_build(settings: Settings, client: httpx.Client) -> str:
    resp = client.get(settings.basemap_builds_url)
    if resp.status_code != 200:
        raise BasemapError(f"Could not list Protomaps builds: HTTP {resp.status_code}")
    builds = [b["key"] for b in resp.json() if str(b.get("key", "")).endswith(".pmtiles")]
    if not builds:
        raise BasemapError("No Protomaps builds listed")
    return sorted(builds)[-1]


def pack_path(settings: Settings, pack_id: int):
    return settings.basemap_dir / f"pack_{pack_id}.pmtiles"


def build_pack(session: Session, settings: Settings, pack_id: int,
               progress: Callable[[float, str], None] | None = None,
               client: httpx.Client | None = None) -> BasemapPack:
    progress = progress or (lambda f, s: None)
    pack = session.get(BasemapPack, pack_id)
    if pack is None:
        raise BasemapError("Basemap pack no longer exists")
    own_client = client is None
    client = client or httpx.Client(timeout=120, follow_redirects=True,
                                    headers={"User-Agent": "signalserver-gui/2 (offline basemap)"})
    try:
        progress(0.02, "finding latest map build")
        build = pack.source or latest_build(settings, client)
        url = build if build.startswith("http") else f"{settings.basemap_source_url}/{build}"
        archive = RemoteArchive(url, client)
        if archive.header["tile_type"] != TileType.MVT:
            raise BasemapError("Basemap source is not a vector tile archive")
        max_zoom = min(pack.max_zoom, archive.header["max_zoom"])
        ids = tile_ids_for(pack.west, pack.south, pack.east, pack.north, max_zoom)
        if len(ids) > settings.basemap_max_tiles:
            raise BasemapError(f"Area needs {len(ids)} tiles (limit {settings.basemap_max_tiles}); "
                               "choose a smaller area or lower max zoom")

        progress(0.05, "reading map index")
        located: list[tuple[int, int, int]] = []
        for i, tid in enumerate(ids):
            loc = archive.locate(tid)
            if loc is not None:
                located.append((tid, *loc))
            if i % 2000 == 0:
                progress(0.05 + 0.25 * i / len(ids), "reading map index")

        # Fetch unique byte ranges, merged into large requests.
        spans = sorted({(off, ln) for _, off, ln in located})
        batches: list[list[tuple[int, int]]] = []
        for off, ln in spans:
            if batches:
                b0 = batches[-1][0][0]
                last_off, last_len = batches[-1][-1]
                if off - (last_off + last_len) <= MERGE_GAP and off + ln - b0 <= MAX_BATCH:
                    batches[-1].append((off, ln))
                    continue
            batches.append([(off, ln)])
        data: dict[int, bytes] = {}
        for i, batch in enumerate(batches):
            start = batch[0][0]
            end = max(o + ln for o, ln in batch)
            blob = archive.get(start, end - start)
            for off, ln in batch:
                data[off] = blob[off - start : off - start + ln]
            progress(0.3 + 0.65 * (i + 1) / len(batches), f"downloading map data {i + 1}/{len(batches)}")

        progress(0.96, "writing pack")
        settings.basemap_dir.mkdir(parents=True, exist_ok=True)
        target = pack_path(settings, pack.id)
        fd, tmp = tempfile.mkstemp(dir=settings.basemap_dir, suffix=".part")
        with os.fdopen(fd, "wb") as fh:
            writer = Writer(fh)
            for tid, off, _ in located:
                writer.write_tile(tid, data[off])
            src = archive.header
            header = {
                "tile_type": src["tile_type"],
                "tile_compression": src["tile_compression"],
                "min_lon_e7": int(pack.west * 1e7), "min_lat_e7": int(pack.south * 1e7),
                "max_lon_e7": int(pack.east * 1e7), "max_lat_e7": int(pack.north * 1e7),
                "center_zoom": min(12, max_zoom),
                "center_lon_e7": int((pack.west + pack.east) / 2 * 1e7),
                "center_lat_e7": int((pack.south + pack.north) / 2 * 1e7),
            }
            meta = archive.metadata()
            meta["name"] = pack.name
            meta["description"] = f"Offline extract of {build}"
            writer.finalize(header, meta)
        os.replace(tmp, target)

        pack.source = build
        pack.max_zoom = max_zoom
        pack.tile_count = len(located)
        pack.size_bytes = target.stat().st_size
        pack.ready = True
        session.commit()
        progress(1.0, "map pack ready")
        return pack
    except httpx.HTTPError as exc:
        raise BasemapError(f"Map download failed: {exc}") from exc
    finally:
        if own_client:
            client.close()


# --- Raster (satellite) packs -------------------------------------------------
def build_raster_pack(session: Session, settings: Settings, pack_id: int,
                      progress: Callable[[float, str], None] | None = None,
                      client: httpx.Client | None = None) -> BasemapPack:
    """Download imagery tiles (``settings.satellite_pack_url`` template) for a pack's area."""
    from concurrent.futures import ThreadPoolExecutor

    from pmtiles.tile import tileid_to_zxy

    progress = progress or (lambda f, s: None)
    pack = session.get(BasemapPack, pack_id)
    if pack is None:
        raise BasemapError("Map pack no longer exists")
    ids = tile_ids_for(pack.west, pack.south, pack.east, pack.north, pack.max_zoom)
    if len(ids) > settings.basemap_max_tiles:
        raise BasemapError(f"Area needs {len(ids)} tiles (limit {settings.basemap_max_tiles})")
    own = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True,
                                    headers={"User-Agent": "signalserver-gui/2 (offline imagery)"})
    template = settings.satellite_pack_url

    def fetch(tid: int) -> tuple[int, bytes | None]:
        z, x, y = tileid_to_zxy(tid)
        for _ in range(3):
            try:
                r = client.get(template.format(z=z, x=x, y=y))
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                return tid, r.content
            if r.status_code in (204, 404):
                return tid, None
        raise BasemapError(f"Imagery tile {z}/{x}/{y} failed to download")

    try:
        tiles: dict[int, bytes] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for i, (tid, data) in enumerate(pool.map(fetch, ids)):
                if data:
                    tiles[tid] = data
                if i % 50 == 0:
                    progress(0.95 * i / len(ids), f"downloading imagery {i}/{len(ids)}")
        fmt = "png" if template.endswith(".png") else "jpg"
        _write_raster_pmtiles(settings, pack, tiles, fmt, {"attribution": settings.satellite_pack_credit})
        pack.source = template.split("/wmts/")[-1].split("/")[2] if "/wmts/" in template else template
        session.commit()
        progress(1.0, "imagery ready")
        return pack
    finally:
        if own:
            client.close()


def import_mbtiles(session: Session, settings: Settings, pack: BasemapPack, path) -> BasemapPack:
    """Convert an uploaded raster MBTiles file (your own imagery) into a pack."""
    import sqlite3

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT name, value FROM metadata").fetchall())
        fmt = (meta.get("format") or "png").lower().replace("jpeg", "jpg")
        if fmt not in ("png", "jpg", "webp"):
            raise BasemapError(f"Only raster MBTiles (png/jpg/webp) are supported, not {fmt!r}")
        tiles: dict[int, bytes] = {}
        for z, x, y_tms, data in con.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"):
            if z > 22:
                continue
            tiles[zxy_to_tileid(z, x, (2**z - 1) - y_tms)] = bytes(data)
    except sqlite3.DatabaseError as exc:
        raise BasemapError(f"Not a valid MBTiles file: {exc}") from exc
    finally:
        con.close()
    if not tiles:
        raise BasemapError("MBTiles file contains no tiles")
    if meta.get("bounds"):
        try:
            pack.west, pack.south, pack.east, pack.north = (float(v) for v in meta["bounds"].split(","))
        except ValueError:
            pass
    zooms = [_zoom_of(t) for t in tiles]
    pack.max_zoom = max(zooms)
    pack.source = f"MBTiles: {meta.get('name', 'imported')}"
    _write_raster_pmtiles(settings, pack, tiles, fmt, {"attribution": meta.get("attribution", "")})
    session.commit()
    return pack


def _zoom_of(tid: int) -> int:
    from pmtiles.tile import tileid_to_zxy

    return tileid_to_zxy(tid)[0]


def _write_raster_pmtiles(settings: Settings, pack: BasemapPack, tiles: dict[int, bytes], fmt: str,
                          meta: dict) -> None:
    settings.basemap_dir.mkdir(parents=True, exist_ok=True)
    target = pack_path(settings, pack.id)
    fd, tmp = tempfile.mkstemp(dir=settings.basemap_dir, suffix=".part")
    with os.fdopen(fd, "wb") as fh:
        writer = Writer(fh)
        for tid in sorted(tiles):
            writer.write_tile(tid, tiles[tid])
        writer.finalize({
            "tile_type": {"jpg": TileType.JPEG, "png": TileType.PNG, "webp": TileType.WEBP}[fmt],
            "tile_compression": Compression.NONE,
            "min_lon_e7": int(pack.west * 1e7), "min_lat_e7": int(pack.south * 1e7),
            "max_lon_e7": int(pack.east * 1e7), "max_lat_e7": int(pack.north * 1e7),
        }, {"name": pack.name, **meta})
    os.replace(tmp, target)
    pack.kind, pack.tile_format = "raster", fmt
    pack.tile_count = len(tiles)
    pack.size_bytes = target.stat().st_size
    pack.ready = True


class PackTileReader:
    """Serve single tiles from a pack's PMTiles file (raster packs are drawn by Cesium directly)."""

    _cache: dict[str, tuple[float, object]] = {}

    @classmethod
    def get(cls, path, z: int, x: int, y: int) -> bytes | None:
        from pmtiles.reader import MmapSource, Reader

        key = str(path)
        mtime = os.path.getmtime(path)
        cached = cls._cache.get(key)
        if cached is None or cached[0] != mtime:
            fh = open(path, "rb")  # kept open for the mmap; replaced when the file changes
            cls._cache[key] = (mtime, Reader(MmapSource(fh)))
        return cls._cache[key][1].get(z, x, y)
