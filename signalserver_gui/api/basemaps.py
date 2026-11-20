"""Offline street-map packs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..basemap import count_tiles, pack_path
from ..config import Settings, get_settings
from ..db import get_db
from ..models import BasemapPack, Job, JobStatus
from ..worker import enqueue
from .resources import _commit, _get

router = APIRouter(prefix="/api/basemaps")


class PackIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    west: float = Field(ge=-180, le=180)
    south: float = Field(ge=-85, le=85)
    east: float = Field(ge=-180, le=180)
    north: float = Field(ge=-85, le=85)
    max_zoom: int = Field(14, ge=6, le=15)
    with_elevation: bool = True

    @model_validator(mode="after")
    def _check(self):
        if self.north <= self.south or self.east <= self.west:
            raise ValueError("Invalid area (west < east and south < north required)")
        return self


def _out(pack: BasemapPack, db: Session) -> dict:
    job = db.get(Job, pack.job_id) if pack.job_id else None
    return {
        "id": pack.id, "name": pack.name, "west": pack.west, "south": pack.south,
        "east": pack.east, "north": pack.north, "max_zoom": pack.max_zoom,
        "tile_count": pack.tile_count, "size_bytes": pack.size_bytes, "ready": pack.ready,
        "source": pack.source, "url": f"/api/basemaps/{pack.id}.pmtiles",
        "created_at": pack.created_at.isoformat() if pack.created_at else None,
        "job": None if job is None else {
            "id": job.id, "status": job.status.value, "stage": job.stage,
            "progress": job.progress, "error": job.error,
        },
    }


@router.get("")
def list_packs(db: Session = Depends(get_db)):
    return [_out(p, db) for p in db.scalars(select(BasemapPack).order_by(BasemapPack.name))]


@router.get("/estimate")
def estimate(west: float, south: float, east: float, north: float, max_zoom: int = 14,
             settings: Settings = Depends(get_settings)):
    tiles = count_tiles(west, south, east, north, max_zoom)
    # Protomaps vector tiles average roughly 20-40 KB at street zooms.
    return {"tiles": tiles, "approx_mb": round(tiles * 30 / 1024), "limit": settings.basemap_max_tiles,
            "ok": tiles <= settings.basemap_max_tiles}


@router.post("", status_code=202)
def create_pack(body: PackIn, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    tiles = count_tiles(body.west, body.south, body.east, body.north, body.max_zoom)
    if tiles > settings.basemap_max_tiles:
        raise HTTPException(422, f"Area needs {tiles} tiles; the limit is {settings.basemap_max_tiles}. "
                                 "Choose a smaller area or a lower max zoom.")
    pack = BasemapPack(name=body.name, west=body.west, south=body.south, east=body.east,
                       north=body.north, max_zoom=body.max_zoom)
    db.add(pack)
    _commit(db, "map pack")
    job = enqueue(db, "basemap_pack", {"pack_id": pack.id})
    pack.job_id = job.id
    elevation_job = None
    if body.with_elevation:
        elevation_job = enqueue(db, "elevation_prefetch", {
            "bbox": [body.west, body.south, body.east, body.north], "model": "dem3"})
    db.commit()
    out = _out(pack, db)
    out["elevation_job_id"] = elevation_job.id if elevation_job else None
    return out


@router.get("/{pack_id}.pmtiles")
def pack_file(pack_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    pack = _get(db, BasemapPack, pack_id)
    path = pack_path(settings, pack.id)
    if not pack.ready or not path.is_file():
        raise HTTPException(404, "Map pack not ready")
    # FileResponse answers HTTP Range requests, which the PMTiles client uses.
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.delete("/{pack_id}", status_code=204)
def delete_pack(pack_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    pack = _get(db, BasemapPack, pack_id)
    if pack.job_id:
        job = db.get(Job, pack.job_id)
        if job is not None and job.status == JobStatus.running:
            raise HTTPException(409, "Pack is still downloading")
    db.delete(pack)
    db.commit()
    pack_path(settings, pack_id).unlink(missing_ok=True)
