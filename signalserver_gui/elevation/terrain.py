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
    def __init__(self, hgt_dir: Path, max_open: int = 48):
        self.hgt_dir = hgt_dir
        self.max_open = max_open
        self._cache: OrderedDict[TileId, np.ndarray | None] = OrderedDict()
        self._lock = threading.Lock()

    def _grid(self, tile: TileId) -> np.ndarray | None:
        with self._lock:
            if tile in self._cache:
                self._cache.move_to_end(tile)
                return self._cache[tile]
        path = self.hgt_dir / tile.hgt_name
        grid = read_hgt(path) if path.exists() else None
        if grid is not None:
            grid = np.where(grid == -32768, 0, grid).astype(np.int16)
        with self._lock:
            self._cache[tile] = grid
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
