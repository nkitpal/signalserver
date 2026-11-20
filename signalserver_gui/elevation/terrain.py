"""Heightmaps for the 3D viewer, sampled from the same DEM tiles used for plots.

CesiumJS's ``CustomHeightmapTerrainProvider`` with a geographic tiling scheme
asks for (x, y, level): level 0 is 2x1 tiles of 180 degrees, y counts from the
north. Each response is ``size x size`` heights, row-major from the north-west
corner, edges inclusive. Areas without a cached tile are at sea level.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

from .tiles import TileId, read_hgt, sample_hgt


class HeightSampler:
    """Samples heights from cached tiles, preferring 1" Copernicus grids over 3" DEM3."""

    def __init__(self, hgt_dir: Path, max_open: int = 48, hgt1_dir: Path | None = None):
        self.hgt_dir = hgt_dir
        self.hgt1_dir = hgt1_dir
        self.max_open = max_open
        self._cache: OrderedDict[Path, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()

    def _grid(self, tile: TileId) -> np.ndarray | None:
        # Pick the best file on every call (cheap stat), so a 1" tile downloaded
        # after a 3" one was cached takes over; cache by file path.
        path = self.hgt_dir / tile.hgt_name
        if self.hgt1_dir is not None and (self.hgt1_dir / tile.hgt_name).exists():
            path = self.hgt1_dir / tile.hgt_name
        with self._lock:
            if path in self._cache:
                self._cache.move_to_end(path)
                return self._cache[path]
        if not path.exists():
            return None  # not cached: re-checked on every request so new downloads show up
        grid = read_hgt(path)
        grid = np.where(grid == -32768, 0, grid).astype(np.int16)
        with self._lock:
            self._cache[path] = grid
            while len(self._cache) > self.max_open:
                self._cache.popitem(last=False)
        return grid

    def forget(self) -> None:
        with self._lock:
            self._cache.clear()

    def sample(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        out = np.zeros(lats.shape, dtype=np.float32)
        tlat = np.clip(np.floor(lats).astype(int), -90, 89)
        tlon = ((np.floor(lons).astype(int) + 180) % 360) - 180
        keys = np.unique(np.stack([tlat.ravel(), tlon.ravel()], axis=1), axis=0)
        for la, lo in keys:
            grid = self._grid(TileId(int(la), int(lo)))
            if grid is None:
                continue
            sel = (tlat == la) & (tlon == lo)
            out[sel] = sample_hgt(grid, TileId(int(la), int(lo)), lats[sel], lons[sel])
        return out

    def heightmap(self, x: int, y: int, level: int, size: int = 32) -> np.ndarray:
        span = 180.0 / (2**level)
        west = -180.0 + x * span
        north = 90.0 - y * span
        lons = np.linspace(west, west + span, size)
        lats = np.linspace(north, north - span, size)
        lon_grid, lat_grid = np.meshgrid(lons, lats)
        # Keep samples inside the valid range of tiles.
        lon_grid = np.clip(lon_grid, -180.0, 179.999999)
        lat_grid = np.clip(lat_grid, -89.999999, 89.999999)
        return self.sample(lat_grid, lon_grid)


def geodesic_profile(points: list[tuple[float, float]], samples: int):
    """Evenly spaced (lat, lon, distance_m) samples along a polyline of (lon, lat) vertices."""
    import math

    r = 6371008.8
    legs = []
    for (lon1, lat1), (lon2, lat2) in zip(points, points[1:]):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dl = math.radians(lon2 - lon1)
        a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        legs.append(2 * r * math.asin(min(1.0, math.sqrt(a))))
    total = sum(legs)
    if total == 0:
        lon, lat = points[0]
        return np.array([lat]), np.array([lon]), np.array([0.0]), 0.0
    dists = np.linspace(0, total, samples)
    lats, lons = np.empty(samples), np.empty(samples)
    start = 0.0
    k = 0
    for i, d in enumerate(dists):
        while k < len(legs) - 1 and d > start + legs[k]:
            start += legs[k]
            k += 1
        f = 0.0 if legs[k] == 0 else min(1.0, (d - start) / legs[k])
        (lon1, lat1), (lon2, lat2) = points[k], points[k + 1]
        # Linear in lat/lon is fine for the short legs drawn on a map.
        lats[i] = lat1 + (lat2 - lat1) * f
        lons[i] = lon1 + (((lon2 - lon1 + 540) % 360) - 180) * f
        lons[i] = ((lons[i] + 180) % 360) - 180
    return lats, lons, dists, total
