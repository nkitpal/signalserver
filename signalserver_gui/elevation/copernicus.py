"""Copernicus DEM GLO-30 (1 arc-second digital *surface* model) from the AWS open data bucket.

Each tile is a Cloud-Optimized GeoTIFF whose pixel centres sit on whole
arc-seconds starting at the tile's north-west corner (``AREA_OR_POINT=Point``):
3600 rows from the north edge down to 1" above the south edge, and 3600 columns
(fewer above 50 degrees latitude) from the west edge. We resample it onto the
3601x3601 HGT-style grid (edges inclusive) used everywhere else.

GLO-30 is a surface model: forests and larger buildings are partly included.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np

from .tiles import TileId

BUCKET_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
HD_SAMPLES = 3601


def tile_name(tile: TileId) -> str:
    ns = "N" if tile.lat >= 0 else "S"
    ew = "E" if tile.lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(tile.lat):02d}_00_{ew}{abs(tile.lon):03d}_00_DEM"


def tile_url(tile: TileId) -> str:
    name = tile_name(tile)
    return f"{BUCKET_URL}/{name}/{name}.tif"


def resample_to_grid(data: np.ndarray, nodata: float | None = None) -> np.ndarray:
    """Copernicus array (3600 rows x W cols) -> int16 3601x3601 grid (row 0 = north edge)."""
    arr = np.asarray(data, dtype=np.float32)
    if nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)
    arr = np.where(np.isfinite(arr), arr, 0.0)
    rows, cols = arr.shape
    # Columns: output column c is lon offset c/3600 deg = source column c * cols / 3600.
    src_c = np.arange(HD_SAMPLES) * (cols / 3600.0)
    c0 = np.clip(np.floor(src_c).astype(int), 0, cols - 1)
    c1 = np.clip(c0 + 1, 0, cols - 1)
    wc = (src_c - np.floor(src_c)).astype(np.float32)
    widened = arr[:, c0] * (1 - wc) + arr[:, c1] * wc
    # Rows already match 1"; repeat the last row for the south edge.
    src_r = np.clip(np.arange(HD_SAMPLES), 0, rows - 1)
    grid = widened[src_r, :]
    return np.rint(grid).astype(np.int16)


def read_cog(path: Path) -> np.ndarray:
    import rasterio

    with rasterio.open(path) as src:
        return resample_to_grid(src.read(1), src.nodata)


def write_hgt(grid: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".part")
    with os.fdopen(fd, "wb") as fh:
        fh.write(grid.astype(">i2").tobytes())
    os.replace(tmp, path)
