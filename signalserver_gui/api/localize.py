"""Transmitter localization from received-power measurements."""

from __future__ import annotations

import shutil
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..localize import loc_dir
from ..models import Job, Localization, Project
from ..worker import enqueue
from .resources import _commit, _get

router = APIRouter(prefix="/api")


class Measurement(BaseModel):
    latitude: float = Field(ge=-70, le=70)
    longitude: float = Field(ge=-180, le=180)
    dbm: float = Field(ge=-200, le=50, description="received power")


class LocalizationIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    method: Literal["terrain", "logdistance"] = "terrain"
    frequency_mhz: float = Field(446.0, ge=20, le=100000)
    tx_height_m: float = Field(10.0, ge=0, le=1000)
    rx_height_m: float = Field(1.5, ge=0, le=1000)
    radius_km: float = Field(15.0, gt=0, le=100)
    elevation_model: Literal["dem3", "cop30", "cop30_buildings"] = "dem3"
    propagation_model: int = Field(1, ge=1, le=12)
    measurements: list[Measurement] = Field(min_length=3, max_length=50)


class LocalizationOut(LocalizationIn):
    id: int
    project_id: int
    status: str
    stage: str
    progress: float
    error: str | None
    result: dict | None
    image_url: str | None
    created_at: datetime


def _out(db: Session, loc: Localization) -> LocalizationOut:
    job = db.get(Job, loc.job_id) if loc.job_id else None
    return LocalizationOut(
        id=loc.id, project_id=loc.project_id, name=loc.name, method=loc.method,
        frequency_mhz=loc.frequency_mhz, tx_height_m=loc.tx_height_m, rx_height_m=loc.rx_height_m,
        radius_km=loc.radius_km, elevation_model=loc.elevation_model,
        propagation_model=loc.propagation_model, measurements=loc.measurements,
        status=job.status.value if job else "done", stage=job.stage if job else "",
        progress=job.progress if job else 1.0, error=job.error if job else None, result=loc.result,
        image_url=f"/api/localizations/{loc.id}/heatmap.png" if loc.result else None,
        created_at=loc.created_at,
    )


def _start(db: Session, loc: Localization) -> None:
    loc.result = None
    job = enqueue(db, "localize", {"localization_id": loc.id})
    loc.job_id = job.id


@router.get("/projects/{project_id}/localizations", response_model=list[LocalizationOut])
def list_localizations(project_id: int, db: Session = Depends(get_db)):
    rows = db.scalars(select(Localization).where(Localization.project_id == project_id).order_by(Localization.id.desc()))
    return [_out(db, x) for x in rows]


@router.post("/projects/{project_id}/localizations", response_model=LocalizationOut, status_code=202)
def create_localization(project_id: int, body: LocalizationIn, db: Session = Depends(get_db)):
    _get(db, Project, project_id)
    data = body.model_dump()
    loc = Localization(project_id=project_id, **data)
    db.add(loc)
    db.flush()
    _start(db, loc)
    _commit(db, "localization")
    return _out(db, loc)


@router.get("/localizations/{loc_id}", response_model=LocalizationOut)
def get_localization(loc_id: int, db: Session = Depends(get_db)):
    return _out(db, _get(db, Localization, loc_id))


@router.put("/localizations/{loc_id}", response_model=LocalizationOut, status_code=202)
def update_localization(loc_id: int, body: LocalizationIn, db: Session = Depends(get_db)):
    """Save changes (e.g. more measurements) and run again."""
    loc = _get(db, Localization, loc_id)
    for k, v in body.model_dump().items():
        setattr(loc, k, v)
    _start(db, loc)
    _commit(db, "localization")
    return _out(db, loc)


@router.get("/localizations/{loc_id}/heatmap.png")
def heatmap(loc_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    loc = _get(db, Localization, loc_id)
    path = loc_dir(settings, loc.id) / "heatmap.png"
    if not loc.result or not path.is_file():
        raise HTTPException(404, "No result yet")
    return FileResponse(path, media_type="image/png")


@router.delete("/localizations/{loc_id}", status_code=204)
def delete_localization(loc_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    loc = _get(db, Localization, loc_id)
    db.delete(loc)
    _commit(db, "localization")
    shutil.rmtree(loc_dir(settings, loc_id), ignore_errors=True)
