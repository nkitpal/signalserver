"""1x1 degree elevation tiles: HGT reading and Signal-Server SDF writing.

Naming conventions
------------------
* HGT (SRTM / Viewfinder): ``N51W001.hgt`` names the tile by its south-west
  corner in signed degrees. DEM3 tiles hold 1201x1201 big-endian int16 samples,
  row 0 = north edge, column 0 = west edge; edges overlap the neighbouring tile.
* SDF (SPLAT!/Signal-Server): ``{min_lat}_{max_lat}_{min_west}_{max_west}.sdf``
  where longitudes are positive *west* degrees. The file is text: a 4 line header
  (max_west, min_north, min_west, max_north) followed by 1200x1200 integers.

Signal-Server picks tile names from its plot box using floor/ceil of the west
longitude without wrapping (an upstream TODO), so near the prime meridian it
asks for tiles such as ``51_52_-1_0`` or ``51_52_360_361``. ``sdf_names`` returns
every name the engine may use for a tile so the provider can write all of them.
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DEM3_SAMPLES = 1201  # 3 arc-second HGT edge length
SDF_IPPD = 1200  # samples per degree in a standard SDF tile
VOID = -32768


@dataclass(frozen=True, order=True)
class TileId:
    """A 1x1 degree tile identified by the signed lat/lon of its SW corner."""

    lat: int
    lon: int

    def __post_init__(self) -> None:
        if not -90 <= self.lat < 90 or not -180 <= self.lon < 180:
            raise ValueError(f"tile out of range: {self.lat}, {self.lon}")

    @property
    def hgt_name(self) -> str:
        ns = "N" if self.lat >= 0 else "S"
        ew = "E" if self.lon >= 0 else "W"
        return f"{ns}{abs(self.lat):02d}{ew}{abs(self.lon):03d}.hgt"

    @property
    def west_min(self) -> int:
        """Western-positive longitude of the tile's east edge, wrapped to 0..359."""
        return (-(self.lon + 1)) % 360

    def sdf_names(self, hd: bool = False) -> list[str]:
        """All SDF file names Signal-Server may request for this tile.

        ``hd``: the 1" (3600 samples/degree) variant read by signalserverHD.
        """
        w = self.west_min
        suffix = "-hd.sdf" if hd else ".sdf"
        westings = [(w, w + 1)]
        if w == 359:
            westings.append((-1, 0))
        if w == 0:
            westings.append((360, 361))
        return [f"{self.lat}_{self.lat + 1}_{a}_{b}{suffix}" for a, b in westings]

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) in signed degrees."""
        return (self.lon, self.lat, self.lon + 1, self.lat + 1)

    @classmethod
    def from_hgt_name(cls, name: str) -> "TileId":
        base = os.path.basename(name).upper()
        if len(base) < 7 or base[0] not in "NS" or base[3] not in "EW":
            raise ValueError(f"not an HGT tile name: {name}")
        lat = int(base[1:3]) * (1 if base[0] == "N" else -1)
        lon = int(base[4:7]) * (1 if base[3] == "E" else -1)
        return cls(lat, lon)


def tiles_for_bbox(west: float, south: float, east: float, north: float) -> list[TileId]:
    """All tiles intersecting a signed-degree bounding box (handles the antimeridian)."""
    south = max(south, -89.999)
    north = min(north, 89.999)
    lats = range(math.floor(south), math.floor(north) + 1)
    if east < west:  # crosses the antimeridian
        spans = [(west, 179.999), (-180.0, east)]
    else:
        spans = [(west, east)]
    out: set[TileId] = set()
    for lo, hi in spans:
        lo = max(lo, -180.0)
        hi = min(hi, 179.999)
        for lon in range(math.floor(lo), math.floor(hi) + 1):
            for lat in lats:
                out.add(TileId(lat, ((lon + 180) % 360) - 180))
    return sorted(out)


def coverage_bbox(lat: float, lon: float, radius_km: float, margin: float = 1.1):
    """Approximate the box Signal-Server loads for a plot (with a safety margin)."""
    dlat = radius_km / 111.0 * margin
    coslat = max(math.cos(math.radians(lat)), 0.05)
    dlon = dlat / coslat
    west = lon - dlon
    east = lon + dlon
    if west < -180:
        west += 360
    if east >= 180:
        east -= 360
    return (west, max(lat - dlat, -89.999), east, min(lat + dlat, 89.999))


def read_hgt(path: Path) -> np.ndarray:
    """Read an HGT file into a square int16 array (row 0 = north)."""
    data = np.fromfile(path, dtype=">i2")
    side = int(round(math.sqrt(data.size)))
    if side * side != data.size:
        raise ValueError(f"{path} is not a square HGT grid ({data.size} samples)")
    return data.reshape(side, side).astype(np.int16)


def fill_voids(grid: np.ndarray) -> np.ndarray:
    """Replace SRTM voids with interpolated values (sea level if all void)."""
    mask = grid == VOID
    if not mask.any():
        return grid
    if mask.all():
        return np.zeros_like(grid)
    from rasterio.fill import fillnodata

    filled = fillnodata(
        grid.astype(np.float32), mask=~mask, max_search_distance=200, smoothing_iterations=0
    )
    filled[filled == VOID] = 0
    return np.rint(filled).astype(np.int16)


def downsample_to_dem3(grid: np.ndarray) -> np.ndarray:
    """3601x3601 (1") grid -> 1201x1201 (3") by taking every third sample."""
    if grid.shape[0] == 3601:
        return grid[::3, ::3]
    return resample_to_dem3(grid)


def resample_to_dem3(grid: np.ndarray) -> np.ndarray:
    """Bring any square grid with overlapping edges to 1201x1201 (nearest sample)."""
    side = grid.shape[0]
    if side == DEM3_SAMPLES:
        return grid
    idx = np.rint(np.linspace(0, side - 1, DEM3_SAMPLES)).astype(int)
    return grid[np.ix_(idx, idx)]


def sdf_body(grid: np.ndarray | None, ippd: int = SDF_IPPD) -> np.ndarray:
    """Return the ippd x ippd SDF sample order for an (ippd+1)^2 HGT-style grid.

    Matches srtm2sdf: rows from south (index ippd) to north (index 1, the
    north edge row 0 is dropped) and, within each row, columns from east
    (index ippd-1, the east edge column is dropped) to west (index 0).
    """
    if grid is None:
        return np.zeros((ippd, ippd), dtype=np.int16)
    if grid.shape[0] != ippd + 1:
        raise ValueError(f"grid is {grid.shape}, expected {ippd + 1} samples per side")
    return grid[ippd:0:-1, ippd - 1 :: -1]


def write_sdf(tile: TileId, grid: np.ndarray | None, out_dir: Path, hd: bool = False) -> list[Path]:
    """Write every SDF name for ``tile``; ``grid=None`` writes a sea-level tile."""
    out_dir.mkdir(parents=True, exist_ok=True)
    body = sdf_body(grid, 3600 if hd else SDF_IPPD)
    payload = "\n".join(map(str, body.ravel().tolist())) + "\n"
    written = []
    for name in tile.sdf_names(hd):
        stem = name[: -len("-hd.sdf")] if hd else name[:-4]
        min_lat, max_lat, min_west, max_west = (int(p) for p in stem.split("_"))
        header = f"{max_west}\n{min_lat}\n{min_west}\n{max_lat}\n"
        target = out_dir / name
        fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".part")
        with os.fdopen(fd, "w") as fh:
            fh.write(header)
            fh.write(payload)
        os.replace(tmp, target)
        written.append(target)
    return written


def sample_hgt(grid: np.ndarray, tile: TileId, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Bilinearly sample heights (metres) at points inside ``tile``."""
    side = grid.shape[0] - 1
    fy = (tile.lat + 1 - lats) * side
    fx = (lons - tile.lon) * side
    fy = np.clip(fy, 0, side)
    fx = np.clip(fx, 0, side)
    y0 = np.floor(fy).astype(int)
    x0 = np.floor(fx).astype(int)
    y1 = np.minimum(y0 + 1, side)
    x1 = np.minimum(x0 + 1, side)
    wy = fy - y0
    wx = fx - x0
    g = grid.astype(np.float32)
    top = g[y0, x0] * (1 - wx) + g[y0, x1] * wx
    bottom = g[y1, x0] * (1 - wx) + g[y1, x1] * wx
    return top * (1 - wy) + bottom * wy
