"""Index of Viewfinder Panoramas DEM3 archives.

Viewfinder Panoramas publishes its 3 arc-second DEM as zip archives, one per
map sheet (mostly 4 degrees of latitude by 6 degrees of longitude, plus a few
regional bundles such as Greenland, Iceland and Antarctica). The coverage map
page is an HTML image map: an 1800x900 pixel equirectangular world map, so
each <area> rectangle converts directly to a lon/lat bounding box at
5 pixels per degree.

The parsed index ships with the package (``resources/viewfinder_dem3_index.json``)
and can be refreshed with ``signalserver-gui elevation refresh-index``.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

COVERAGE_PAGE_URL = (
    "https://www.viewfinderpanoramas.org/Coverage%20map%20viewfinderpanoramas_org3.htm"
)
MAP_WIDTH_PX = 1800
PX_PER_DEGREE = MAP_WIDTH_PX / 360.0

_AREA_RE = re.compile(r"<area\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


@dataclass(frozen=True)
class Archive:
    """One downloadable DEM3 zip and the area it covers."""

    name: str
    url: str
    west: float
    south: float
    east: float
    north: float

    def contains(self, lon: float, lat: float) -> bool:
        return self.west <= lon < self.east and self.south <= lat < self.north

    @property
    def area(self) -> float:
        return (self.east - self.west) * (self.north - self.south)


def parse_coverage_page(html: str) -> list[Archive]:
    """Parse the coverage map's <area> elements into archives.

    The same zip can appear in several rectangles (irregular regions are drawn
    as multiple boxes); each rectangle becomes its own entry.
    """
    archives: list[Archive] = []
    seen: set[tuple] = set()
    for tag in _AREA_RE.findall(html):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        href = attrs.get("href", "")
        coords = attrs.get("coords", "")
        if not href.lower().endswith(".zip") or attrs.get("shape", "rect") != "rect":
            continue
        try:
            x1, y1, x2, y2 = (float(c) for c in coords.split(","))
        except ValueError:
            continue
        url = href.replace("http://", "https://", 1)
        name = url.rsplit("/", 1)[-1][:-4]
        west = round(min(x1, x2) / PX_PER_DEGREE - 180.0, 4)
        east = round(max(x1, x2) / PX_PER_DEGREE - 180.0, 4)
        north = round(90.0 - min(y1, y2) / PX_PER_DEGREE, 4)
        south = round(90.0 - max(y1, y2) / PX_PER_DEGREE, 4)
        key = (url, west, south, east, north)
        if key in seen:
            continue
        seen.add(key)
        archives.append(Archive(name, url, west, south, east, north))
    return archives


def save_index(archives: list[Archive], path: Path) -> None:
    path.write_text(json.dumps([asdict(a) for a in archives], indent=0) + "\n")


def _load(path: Path | None) -> list[Archive]:
    if path is not None and path.is_file():
        raw = json.loads(path.read_text())
    else:
        ref = resources.files("signalserver_gui.resources").joinpath(
            "viewfinder_dem3_index.json"
        )
        raw = json.loads(ref.read_text())
    return [Archive(**item) for item in raw]


@lru_cache(maxsize=4)
def load_index(path: str | None = None) -> tuple[Archive, ...]:
    """Load the archive index (an override file, or the bundled copy)."""
    return tuple(_load(Path(path) if path else None))


def archives_for_tile(
    lat: int, lon: int, index: tuple[Archive, ...] | None = None
) -> list[Archive]:
    """Return archives that may hold the 1x1 degree tile with SW corner (lat, lon).

    Smaller (more specific) sheets come first; regional bundles last.
    """
    index = index if index is not None else load_index()
    cx, cy = lon + 0.5, lat + 0.5
    hits = [a for a in index if a.contains(cx, cy)]
    return sorted(hits, key=lambda a: (a.area, a.name))
