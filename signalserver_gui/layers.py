"""KML / KMZ parsing for imported layers."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

MAX_BYTES = 50 * 1024 * 1024


class LayerError(ValueError):
    pass


@dataclass
class ParsedKml:
    name: str = ""
    points: list[dict] = field(default_factory=list)  # {name, latitude, longitude, altitude, description}
    feature_count: int = 0
    bounds: tuple[float, float, float, float] | None = None  # west, south, east, north


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _coords(text: str) -> list[tuple[float, float, float]]:
    out = []
    for chunk in (text or "").split():
        parts = chunk.split(",")
        if len(parts) >= 2:
            try:
                lon, lat = float(parts[0]), float(parts[1])
                alt = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
            except ValueError:
                continue
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                out.append((lon, lat, alt))
    return out


def kml_bytes(data: bytes, filename: str) -> bytes:
    """Return the KML document from a .kml or .kmz upload."""
    if len(data) > MAX_BYTES:
        raise LayerError("File is larger than 50 MB")
    if filename.lower().endswith(".kmz") or data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
                if not names:
                    raise LayerError("KMZ contains no .kml document")
                main = "doc.kml" if "doc.kml" in names else sorted(names, key=len)[0]
                return zf.read(main)
        except zipfile.BadZipFile as exc:
            raise LayerError("Not a valid KMZ file") from exc
    return data


def parse_kml(doc: bytes) -> ParsedKml:
    # ElementTree does not resolve external entities; reject DTDs outright anyway.
    if b"<!DOCTYPE" in doc[:2000].upper() or b"<!ENTITY" in doc.upper():
        raise LayerError("KML with DTD/entities is not supported")
    try:
        root = ET.fromstring(doc)
    except ET.ParseError as exc:
        raise LayerError(f"Invalid KML: {exc}") from exc
    result = ParsedKml()
    xs: list[float] = []
    ys: list[float] = []
    for el in root.iter():
        tag = _local(el.tag)
        if tag in ("Document", "Folder") and not result.name:
            name = next((c for c in el if _local(c.tag) == "name"), None)
            if name is not None and name.text:
                result.name = name.text.strip()
        if tag != "Placemark":
            continue
        result.feature_count += 1
        pname = ""
        desc = ""
        for child in el:
            if _local(child.tag) == "name" and child.text:
                pname = child.text.strip()
            elif _local(child.tag) == "description" and child.text:
                desc = child.text.strip()
        for geom in el.iter():
            gtag = _local(geom.tag)
            if gtag != "coordinates":
                continue
            pts = _coords(geom.text or "")
            xs += [p[0] for p in pts]
            ys += [p[1] for p in pts]
        point = next((g for g in el.iter() if _local(g.tag) == "Point"), None)
        if point is not None:
            coords = next((c for c in point.iter() if _local(c.tag) == "coordinates"), None)
            pts = _coords(coords.text if coords is not None else "")
            if pts:
                lon, lat, alt = pts[0]
                result.points.append({"name": pname or f"Point {len(result.points) + 1}",
                                      "latitude": lat, "longitude": lon, "altitude": alt,
                                      "description": desc[:2000]})
    if xs:
        result.bounds = (min(xs), min(ys), max(xs), max(ys))
    return result
