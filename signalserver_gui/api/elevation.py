"""Elevation data endpoints and the 3D viewer's terrain feed."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Response
from geoalchemy2.functions import ST_Intersects, ST_MakeEnvelope
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..elevation.index import archives_for_tile, load_index
from ..elevation.terrain import HeightSampler, geodesic_profile
from ..elevation.tiles import tiles_for_bbox
from ..models import ElevationArchive, ElevationTile
from ..schemas import JobOut, PrefetchIn
from ..worker import MAX_PREFETCH_TILES, enqueue

router = APIRouter(prefix="/api/elevation")

TERRAIN_SIZE = 32


@lru_cache
def _sampler(hgt_dir: str, hgt1_dir: str) -> HeightSampler:
    from pathlib import Path

    return HeightSampler(Path(hgt_dir), hgt1_dir=Path(hgt1_dir))


def sampler(settings: Settings = Depends(get_settings)) -> HeightSampler:
    return _sampler(str(settings.hgt_dir), str(settings.hgt1_dir))


MODEL_INFO = {
    "dem3": "Viewfinder Panoramas DEM3 (3 arc-second, ~90 m, terrain)",
    "cop30": "Copernicus GLO-30 (1 arc-second, ~30 m, surface incl. forest/buildings)",
    "cop30_buildings": "Copernicus GLO-30 + OpenStreetMap building heights",
}


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    rows = db.execute(
        select(ElevationTile.model, func.count(), func.count().filter(ElevationTile.has_data),
               func.coalesce(func.sum(ElevationTile.building_count), 0))
        .group_by(ElevationTile.model)
    ).all()
    by_model = {m: {"tiles": t, "tiles_with_data": d, "buildings": int(b)} for m, t, d, b in rows}
    archives = db.scalar(select(func.count()).select_from(ElevationArchive))
    dem3 = by_model.get("dem3", {"tiles": 0, "tiles_with_data": 0})
    return {"tiles": dem3["tiles"], "tiles_with_data": dem3["tiles_with_data"], "archives": archives,
            "source": MODEL_INFO["dem3"],
            "models": {m: {"description": MODEL_INFO[m], **by_model.get(m, {"tiles": 0, "tiles_with_data": 0, "buildings": 0})}
                       for m in MODEL_INFO}}


@router.get("/tiles.geojson")
def tiles_geojson(
    west: float = -180, south: float = -90, east: float = 180, north: float = 90,
    model: str = "dem3", db: Session = Depends(get_db),
):
    stmt = select(ElevationTile).where(
        ElevationTile.model == model,
        ST_Intersects(ElevationTile.footprint, ST_MakeEnvelope(west, south, east, north, 4326)),
    ).limit(5000)
    feats = []
    for t in db.scalars(stmt):
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[
                [t.lon, t.lat], [t.lon + 1, t.lat], [t.lon + 1, t.lat + 1], [t.lon, t.lat + 1], [t.lon, t.lat]
            ]]},
            "properties": {"lat": t.lat, "lon": t.lon, "has_data": t.has_data, "source": t.source,
                           "model": t.model, "buildings": t.building_count,
                           "min": t.min_elevation, "max": t.max_elevation},
        })
    return {"type": "FeatureCollection", "features": feats}


@router.get("/archives")
def archives(lat: int | None = None, lon: int | None = None, db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)):
    """Viewfinder DEM3 archives (all, or those covering one tile) and whether downloaded."""
    index = load_index(str(settings.viewfinder_index_path) if settings.viewfinder_index_path else None)
    items = archives_for_tile(lat, lon, index) if lat is not None and lon is not None else list(index)
    done = set(db.scalars(select(ElevationArchive.name)))
    return [{**a.__dict__, "downloaded": a.name in done} for a in items]


@router.post("/prefetch", response_model=JobOut, status_code=202)
def prefetch(body: PrefetchIn, db: Session = Depends(get_db)):
    count = len(tiles_for_bbox(body.west, body.south, body.east, body.north))
    # Copernicus tiles are ~40 MB each; the buildings model also queries Overpass 16x per tile.
    limit = {"dem3": MAX_PREFETCH_TILES, "cop30": 64, "cop30_buildings": 9}[body.model]
    if count > limit:
        raise HTTPException(422, f"Area covers {count} tiles; the limit for this model is {limit}")
    job = enqueue(db, "elevation_prefetch", {"bbox": [body.west, body.south, body.east, body.north],
                                             "model": body.model, "hd": body.model != "dem3"})
    db.commit()
    out = JobOut.model_validate(job)
    out.status = job.status.value
    return out


@router.get("/height")
def height(lat: float, lon: float, s: HeightSampler = Depends(sampler)):
    if not (-90 <= lat < 90 and -180 <= lon < 180):
        raise HTTPException(422, "coordinates out of range")
    h = s.sample(np.array([lat]), np.array([lon]))[0]
    return {"latitude": lat, "longitude": lon, "height_m": round(float(h), 1)}


@router.get("/profile")
def profile(path: str, samples: int = 200, s: HeightSampler = Depends(sampler)):
    """Terrain profile along ``path`` = "lon,lat;lon,lat;..." (from cached elevation tiles)."""
    try:
        points = [tuple(float(v) for v in p.split(",")[:2]) for p in path.split(";") if p.strip()]
    except ValueError as exc:
        raise HTTPException(422, "path must be 'lon,lat;lon,lat;...'") from exc
    if len(points) < 2 or len(points) > 500:
        raise HTTPException(422, "path needs 2 to 500 points")
    if any(not (-180 <= lon <= 180 and -90 < lat < 90) for lon, lat in points):
        raise HTTPException(422, "coordinates out of range")
    samples = max(2, min(samples, 2000))
    lats, lons, dists, total = geodesic_profile(points, samples)
    heights = s.sample(lats, lons)
    return {"distance_m": round(total, 1),
            "points": [[round(float(d), 1), round(float(h), 1), round(float(la), 6), round(float(lo), 6)]
                       for d, h, la, lo in zip(dists, heights, lats, lons)]}


@router.get("/terrain/{level}/{x}/{y}.f32")
def terrain_tile(level: int, x: int, y: int, s: HeightSampler = Depends(sampler)):
    """Float32 little-endian ``32x32`` heightmap for Cesium (geographic tiling)."""
    if not (0 <= level <= 16 and 0 <= x < 2 ** (level + 1) and 0 <= y < 2**level):
        raise HTTPException(404, "tile out of range")
    heights = s.heightmap(x, y, level, TERRAIN_SIZE).astype("<f4")
    return Response(heights.tobytes(), media_type="application/octet-stream",
                    headers={"Cache-Control": "public, max-age=300"})
