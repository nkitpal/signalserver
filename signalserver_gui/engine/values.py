"""Recover per-pixel values from Signal-Server's colour image.

Signal-Server only writes a colour PPM where each pixel takes the colour of the
palette band its value falls in. We run it with a *measurement palette* of 128
bands, 1 unit apart, each with a unique colour, so every pixel can be decoded
back to the engine's integer value (dBm, dBuV/m or dB path loss). This value
raster then drives:

* the display PNG (coloured with the user's palette),
* ``values.png``: 8-bit band index per pixel for the browser (hover read-outs,
  best-server / overlap composites across plots),
* ``values.tif``: Int16 GeoTIFF of real values for GIS tools.

Band index ``i`` decodes to ``value_top + i * value_step``; index 255 = no data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

LEVELS = 128
NODATA_INDEX = 255
NODATA_VALUE = -32768
_R0, _G, _B = 10, 200, 50  # band i -> rgb(10 + i, 200, 50): unique, never grey/white/black


@dataclass(frozen=True)
class Quantity:
    kind: str  # "dbm" | "dbuvm" | "loss"
    unit: str
    ext: str  # palette extension the engine expects
    top: int  # value of band 0
    step: int  # value change per band
    descending: bool  # engine matches bands from strong to weak

    def value(self, index: np.ndarray) -> np.ndarray:
        return self.top + index.astype(np.int32) * self.step


QUANTITIES = {
    # dBm: band 0 = >= -30 dBm, band i = [-30 - i, -29 - i)
    "dbm": Quantity("dbm", "dBm", ".dcf", -30, -1, True),
    # field strength: band 0 = >= 130 dBuV/m
    "dbuvm": Quantity("dbuvm", "dBuV/m", ".scf", 130, -1, True),
    # path loss (2 dB bands to span 70-324 dB): band 0 = <= 70 dB, band i = [68 + 2i, 70 + 2i)
    "loss": Quantity("loss", "dB", ".lcf", 68, 2, False),
}


def quantity_for(params: dict) -> Quantity:
    if params.get("erp_watts", 1) <= 0:
        return QUANTITIES["loss"]
    return QUANTITIES["dbm" if params.get("use_dbm") else "dbuvm"]


def write_measurement_palette(q: Quantity, run_dir: Path) -> str:
    """Write the 128-band palette into ``run_dir``; returns the *relative* name.

    The engine truncates the -color path at its first '.', so the file must be
    passed without directories that might contain dots.
    """
    name = f"measure{q.ext}"
    lines = []
    for i in range(LEVELS):
        level = (q.top + i * q.step) if q.descending else (q.top + (i + 1) * q.step)
        lines.append(f"{level:+d}: {_R0 + i}, {_G}, {_B}")
    (run_dir / name).write_text("\n".join(lines) + "\n")
    return name


def decode_indices(rgb: np.ndarray) -> np.ndarray:
    """Map measurement-palette colours to band indices (255 where not coverage)."""
    r = rgb[..., 0].astype(np.int16)
    hit = (rgb[..., 1] == _G) & (rgb[..., 2] == _B) & (r >= _R0) & (r < _R0 + LEVELS)
    out = np.full(rgb.shape[:2], NODATA_INDEX, dtype=np.uint8)
    out[hit] = (r[hit] - _R0).astype(np.uint8)
    return out


def index_to_values(index: np.ndarray, q: Quantity) -> np.ndarray:
    values = q.value(index).astype(np.int16)
    values[index == NODATA_INDEX] = NODATA_VALUE
    return values


def colorize(index: np.ndarray, q: Quantity, palette: list[dict]) -> np.ndarray:
    """RGBA image of the band indices coloured with a user palette (same band rules as the engine)."""
    h, w = index.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    if not palette:
        return rgba
    levels = np.array([p["value"] for p in palette], dtype=np.float64)
    colors = np.array(
        [[int(p["color"][i : i + 2], 16) for i in (1, 3, 5)] for p in palette], dtype=np.uint8
    )
    valid = index != NODATA_INDEX
    vals = q.value(index).astype(np.float64)
    if q.descending:
        # Engine: band 0 if v >= L0, else band z with L(z) <= v < L(z-1):
        # the first level (strong -> weak) that the value reaches.
        order = np.argsort(-levels)
        lv = levels[order]
        band = np.searchsorted(-lv, -vals, side="left")
        inside = vals >= lv[-1]
    else:
        # Engine: band 0 if v <= L0, else band z with L(z-1) <= v < L(z).
        order = np.argsort(levels)
        lv = levels[order]
        band = np.where(vals <= lv[0], 0, np.searchsorted(lv, vals, side="right"))
        inside = vals < lv[-1]
    band = np.clip(band, 0, len(lv) - 1)
    show = valid & inside
    rgba[show, :3] = colors[order][band[show]]
    rgba[show, 3] = 255
    return rgba
