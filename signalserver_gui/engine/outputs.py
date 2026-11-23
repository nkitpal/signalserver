"""Turn Signal-Server's PPM output into web/GIS friendly files."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import simplekml
from PIL import Image

_BOUNDS_RE = re.compile(
    r"Area boundaries:\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)"
)


def parse_bounds(engine_output: str) -> tuple[float, float, float, float] | None:
    """Return (north, east, south, west) printed by the engine, if valid."""
    m = None
    for m in _BOUNDS_RE.finditer(engine_output):
        pass
    if m is None:
        return None
    north, east, south, west = (float(g) for g in m.groups())
    if not (-90 <= south < north <= 90 and -180 <= west <= 180 and -180 <= east <= 180):
        return None
    width = (east - west) % 360
    if width <= 0 or width > 20 or north - south > 20:
        return None
    return north, east, south, west


def read_rgb(ppm: Path) -> np.ndarray:
    with Image.open(ppm) as img:
        return np.asarray(img.convert("RGB"))


def write_index_png(index: np.ndarray, path: Path) -> None:
    """8-bit greyscale band indices (255 = no data) for the browser."""
    Image.fromarray(index, "L").save(path, optimize=True)


def write_values_tif(values: np.ndarray, bounds: tuple[float, float, float, float], path: Path,
                     unit: str) -> None:
    """Int16 GeoTIFF (EPSG:4326) of decoded values, nodata -32768."""
    import rasterio
    from rasterio.transform import from_bounds

    north, east, south, west = bounds
    height, width = values.shape
    profile = dict(driver="GTiff", width=width, height=height, count=1, dtype="int16",
                   crs="EPSG:4326", transform=from_bounds(west, south, east, north, width, height),
                   nodata=-32768, compress="deflate", tiled=True, blockxsize=256, blockysize=256)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values, 1)
        dst.update_tags(1, unit=unit)
        dst.set_band_description(1, f"signal ({unit})")


def ppm_to_rgba(ppm: Path) -> np.ndarray:
    """Load the engine image; white (no coverage) becomes transparent."""
    with Image.open(ppm) as img:
        rgb = np.asarray(img.convert("RGB"))
    alpha = np.where(np.all(rgb == 255, axis=2), 0, 255).astype(np.uint8)
    return np.dstack([rgb, alpha])


def write_png(rgba: np.ndarray, path: Path) -> None:
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)


def write_cog(rgba: np.ndarray, bounds: tuple[float, float, float, float], path: Path) -> None:
    """Write a Cloud-Optimized GeoTIFF (EPSG:4326, RGBA)."""
    import rasterio
    from rasterio.transform import from_bounds

    north, east, south, west = bounds
    height, width = rgba.shape[:2]
    transform = from_bounds(west, south, east, north, width, height)
    profile = dict(
        driver="COG",
        width=width,
        height=height,
        count=4,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
        compress="deflate",
    )
    try:
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(np.moveaxis(rgba, 2, 0))
            dst.colorinterp = _rgba_interp()
    except Exception:  # GDAL without COG driver: plain tiled GeoTIFF
        profile.update(driver="GTiff", tiled=True, blockxsize=256, blockysize=256, photometric="RGB")
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(np.moveaxis(rgba, 2, 0))
            dst.colorinterp = _rgba_interp()


def _rgba_interp():
    from rasterio.enums import ColorInterp

    return [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]


def write_kmz(
    path: Path,
    *,
    name: str,
    image_name: str,
    image_path: Path,
    bounds: tuple[float, float, float, float],
    opacity: float,
    tx: dict,
    rx: dict | None = None,
    report: dict | None = None,
) -> None:
    north, east, south, west = bounds
    kml = simplekml.Kml(name=name)
    kml.addfile(str(image_path))
    overlay = kml.newgroundoverlay(name=f"Coverage: {name}")
    overlay.icon.href = image_name
    overlay.latlonbox.north, overlay.latlonbox.south = north, south
    overlay.latlonbox.east, overlay.latlonbox.west = east, west
    overlay.color = simplekml.Color.changealphaint(int(round(opacity * 255)), simplekml.Color.white)

    def pin(site: dict, label: str, color: str):
        pt = kml.newpoint(name=f"{label}: {site['name']}", coords=[(site["longitude"], site["latitude"], site["height_m"])])
        pt.altitudemode = simplekml.AltitudeMode.relativetoground
        pt.extrude = 1
        pt.style.iconstyle.icon.href = f"http://maps.google.com/mapfiles/kml/pushpin/{color}-pushpin.png"
        pt.lookat = simplekml.LookAt(
            latitude=site["latitude"], longitude=site["longitude"], range=max(2000, (north - south) * 111000 * 2),
            tilt=45, heading=0, altitudemode=simplekml.AltitudeMode.relativetoground,
        )
        return pt

    pin(tx, "Transmitter", "grn")
    if rx is not None:
        pin(rx, "Receiver", "red")
        line = kml.newlinestring(name="Path")
        line.coords = [
            (tx["longitude"], tx["latitude"], tx["height_m"]),
            (rx["longitude"], rx["latitude"], rx["height_m"]),
        ]
        line.altitudemode = simplekml.AltitudeMode.relativetoground
        clear = bool(report and report.get("line_of_sight_clear"))
        line.style.linestyle.color = simplekml.Color.green if clear else simplekml.Color.red
        line.style.linestyle.width = 3
        for i, obs in enumerate((report or {}).get("obstructions", []), 1):
            kml.newpoint(name=f"Obstruction {i}", coords=[(obs["longitude"], obs["latitude"])])
    kml.savekmz(str(path))
