"""OpenStreetMap building heights burned into a 1" surface model.

Buildings come from the Overpass API in 0.25 degree chunks (cached on disk).
Height: the ``height`` tag, else ``building:levels`` x 3 m, else a default.
Each building raises the grid cells it covers (or, for buildings smaller than
a cell, the cell holding its centroid) to ``local ground + height``, where
local ground is the minimum of the surface model in a 5x5 (~150 m) window.
Cells the surface model already shows higher are left alone, so buildings that
GLO-30 already captures are not counted twice.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

import httpx
import numpy as np

from .tiles import TileId

log = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
CHUNKS = 4  # per degree, each way
LEVEL_HEIGHT_M = 3.0
_NUM = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(m|meters?|metres?|ft|feet|')?\s*$", re.I)


class BuildingsError(RuntimeError):
    pass


def parse_height(tags: dict, default: float) -> float:
    raw = tags.get("height") or tags.get("building:height")
    if raw:
        m = _NUM.match(str(raw))
        if m:
            value = float(m.group(1).replace(",", "."))
            if (m.group(2) or "").lower() in ("ft", "feet", "'"):
                value *= 0.3048
            if 0 < value < 1000:
                return value
    levels = tags.get("building:levels")
    if levels:
        try:
            n = float(str(levels).split(";")[0].replace(",", "."))
            if 0 < n < 200:
                return n * LEVEL_HEIGHT_M + (1.0 if tags.get("roof:levels") else 0.0)
        except ValueError:
            pass
    return default


def overpass_query(south: float, west: float, north: float, east: float) -> str:
    bbox = f"{south},{west},{north},{east}"
    return (
        "[out:json][timeout:180];"
        f'(way["building"]({bbox});relation["building"]["type"="multipolygon"]({bbox}););'
        "out tags geom;"
    )


def parse_overpass(data: dict, default_height: float) -> list[tuple[list[tuple[float, float]], float]]:
    """Return (outer ring as [(lon, lat)], height) for each building."""
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        h = parse_height(tags, default_height)
        rings = []
        if el.get("type") == "way" and el.get("geometry"):
            rings.append(el["geometry"])
        elif el.get("type") == "relation":
            for m in el.get("members", []):
                if m.get("role") == "outer" and m.get("geometry"):
                    rings.append(m["geometry"])
        for ring in rings:
            pts = [(p["lon"], p["lat"]) for p in ring if "lon" in p]
            if len(pts) >= 3:
                out.append((pts, h))
    return out


class BuildingSource:
    def __init__(self, cache_dir: Path, client: httpx.Client | None = None,
                 default_height: float = 6.0, url: str = OVERPASS_URL):
        self.cache_dir = cache_dir
        self.client = client
        self.default_height = default_height
        self.url = url

    def _http(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(timeout=300, headers={"User-Agent": "signalserver-gui/2 (buildings)"})
        return self.client

    def chunk(self, tile: TileId, i: int, j: int) -> dict:
        path = self.cache_dir / f"{tile.lat}_{tile.lon}_{i}_{j}.json.gz"
        if path.exists():
            return json.loads(gzip.decompress(path.read_bytes()))
        size = 1.0 / CHUNKS
        south, west = tile.lat + i * size, tile.lon + j * size
        query = overpass_query(south, west, south + size, west + size)
        try:
            resp = self._http().post(self.url, data={"data": query})
        except httpx.HTTPError as exc:
            raise BuildingsError(f"Overpass request failed: {exc}") from exc
        if resp.status_code != 200:
            raise BuildingsError(f"Overpass returned HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if "remark" in data and "runtime error" in str(data["remark"]).lower():
            raise BuildingsError(f"Overpass error: {data['remark']}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(json.dumps(data).encode()))
        return data

    def buildings(self, tile: TileId, progress: Callable[[float], None] | None = None):
        out = []
        n = CHUNKS * CHUNKS
        for k in range(n):
            i, j = divmod(k, CHUNKS)
            out.extend(parse_overpass(self.chunk(tile, i, j), self.default_height))
            if progress:
                progress((k + 1) / n)
        return out


def burn_buildings(grid: np.ndarray, tile: TileId, buildings) -> tuple[np.ndarray, int]:
    """Raise a 1" surface grid (3601x3601, row 0 = north) by OSM buildings."""
    from rasterio.features import rasterize
    from rasterio.transform import from_origin

    if not buildings:
        return grid, 0
    side = grid.shape[0]
    res = 1.0 / (side - 1)
    transform = from_origin(tile.lon - res / 2, tile.lat + 1 + res / 2, res, res)
    shapes = sorted(
        (({"type": "Polygon", "coordinates": [pts]}, h) for pts, h in buildings), key=lambda s: s[1]
    )
    heights = rasterize(shapes, out_shape=grid.shape, transform=transform, fill=0,
                        dtype="float32", all_touched=False)
    # Buildings smaller than a cell: mark the cell containing their centroid.
    for pts, h in buildings:
        arr = np.asarray(pts)
        cx, cy = arr[:, 0].mean(), arr[:, 1].mean()
        c = int(round((cx - tile.lon) / res))
        r = int(round((tile.lat + 1 - cy) / res))
        if 0 <= r < side and 0 <= c < side and heights[r, c] < h:
            heights[r, c] = h

    g = grid.astype(np.float32)
    pad = np.pad(g, 2, mode="edge")
    from numpy.lib.stride_tricks import sliding_window_view

    ground = sliding_window_view(pad, (5, 5)).min(axis=(2, 3))
    raised = np.where(heights > 0, np.maximum(g, ground + heights), g)
    return np.rint(raised).astype(np.int16), len(buildings)
