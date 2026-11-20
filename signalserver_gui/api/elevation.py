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
from ..elevation.terrain import HeightSampler
from ..elevation.tiles import tiles_for_bbox
from ..models import ElevationArchive, ElevationTile
from ..schemas import JobOut, PrefetchIn
from ..worker import MAX_PREFETCH_TILES, enqueue

router = APIRouter(prefix="/api/elevation")

TERRAIN_SIZE = 32


@lru_cache
def _sampler(hgt_dir: str) -> HeightSampler:
    from pathlib import Path

    return HeightSampler(Path(hgt_dir))


def sampler(settings: Settings = Depends(get_settings)) -> HeightSampler:
    return _sampler(str(settings.hgt_dir))


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    total, with_data = db.execute(
        select(func.count(), func.count().filter(ElevationTile.has_data))
    ).one()
    archives = db.scalar(select(func.count()).select_from(ElevationArchive))
    return {"tiles": total, "tiles_with_data": with_data, "archives": archives,
            "source": "Viewfinder Panoramas DEM3 (3 arc-second)"}


@router.get("/tiles.geojson")
def tiles_geojson(
    west: float = -180, south: float = -90, east: float = 180, north: float = 90,
    db: Session = Depends(get_db),
):
    stmt = select(ElevationTile).where(
        ST_Intersects(ElevationTile.footprint, ST_MakeEnvelope(west, south, east, north, 4326))
    ).limit(5000)
    feats = []
    for t in db.scalars(stmt):
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[
                [t.lon, t.lat], [t.lon + 1, t.lat], [t.lon + 1, t.lat + 1], [t.lon, t.lat + 1], [t.lon, t.lat]
            ]]},
            "properties": {"lat": t.lat, "lon": t.lon, "has_data": t.has_data, "source": t.source,
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
    if count > MAX_PREFETCH_TILES:
        raise HTTPException(422, f"Area covers {count} tiles; the limit is {MAX_PREFETCH_TILES}")
    job = enqueue(db, "elevation_prefetch", {"bbox": [body.west, body.south, body.east, body.north]})
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


@router.get("/terrain/{level}/{x}/{y}.f32")
def terrain_tile(level: int, x: int, y: int, s: HeightSampler = Depends(sampler)):
    """Float32 little-endian ``32x32`` heightmap for Cesium (geographic tiling)."""
    if not (0 <= level <= 16 and 0 <= x < 2 ** (level + 1) and 0 <= y < 2**level):
        raise HTTPException(404, "tile out of range")
    heights = s.heightmap(x, y, level, TERRAIN_SIZE).astype("<f4")
    return Response(heights.tobytes(), media_type="application/octet-stream",
                    headers={"Cache-Control": "public, max-age=300"})
