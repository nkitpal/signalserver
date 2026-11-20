"""CRUD endpoints for stations, antennas and plots."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..engine.patterns import remove_antenna_pattern, safe_name, save_antenna_pattern
from ..models import Antenna, Plot, PlotRun, Station
from ..schemas import (
    AntennaOut,
    AntennaUpdate,
    PlotIn,
    PlotOut,
    StationIn,
    StationOut,
)
from .runs import run_summary

router = APIRouter(prefix="/api")


def _commit(db: Session, what: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        msg = str(exc.orig).splitlines()[0] if exc.orig else str(exc)
        if "unique" in msg.lower():
            raise HTTPException(409, f"A {what} with that name already exists") from exc
        if "foreign key" in msg.lower():
            raise HTTPException(409, f"This {what} is used by a plot") from exc
        raise HTTPException(422, msg) from exc


def _get(db: Session, model, item_id: int):
    obj = db.get(model, item_id)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {item_id} not found")
    return obj


# Stations -------------------------------------------------------------------
def _station_out(s: Station) -> StationOut:
    return StationOut(
        id=s.id, name=s.name, latitude=s.latitude, longitude=s.longitude, height_m=s.height_m,
        polarization=s.polarization, rotation_deg=s.rotation_deg, downtilt_deg=s.downtilt_deg,
        downtilt_direction_deg=s.downtilt_direction_deg, notes=s.notes,
        created_at=s.created_at, updated_at=s.updated_at,
    )


@router.get("/stations", response_model=list[StationOut])
def list_stations(q: str = "", db: Session = Depends(get_db)):
    stmt = select(Station).order_by(Station.name)
    if q:
        stmt = stmt.where(Station.name.ilike(f"%{q}%"))
    return [_station_out(s) for s in db.scalars(stmt)]


@router.get("/stations.geojson")
def stations_geojson(db: Session = Depends(get_db)):
    feats = []
    for s in db.scalars(select(Station).order_by(Station.name)):
        o = _station_out(s)
        feats.append({
            "type": "Feature",
            "id": s.id,
            "geometry": {"type": "Point", "coordinates": [o.longitude, o.latitude, o.height_m]},
            "properties": o.model_dump(mode="json"),
        })
    return {"type": "FeatureCollection", "features": feats}


@router.post("/stations", response_model=StationOut, status_code=201)
def create_station(body: StationIn, db: Session = Depends(get_db)):
    data = body.model_dump()
    lat, lon = data.pop("latitude"), data.pop("longitude")
    station = Station(**data)
    station.set_position(lat, lon)
    db.add(station)
    _commit(db, "station")
    return _station_out(station)


@router.get("/stations/{station_id}", response_model=StationOut)
def get_station(station_id: int, db: Session = Depends(get_db)):
    return _station_out(_get(db, Station, station_id))


@router.put("/stations/{station_id}", response_model=StationOut)
def update_station(station_id: int, body: StationIn, db: Session = Depends(get_db)):
    station = _get(db, Station, station_id)
    data = body.model_dump()
    station.set_position(data.pop("latitude"), data.pop("longitude"))
    for k, v in data.items():
        setattr(station, k, v)
    _commit(db, "station")
    return _station_out(station)


@router.delete("/stations/{station_id}", status_code=204)
def delete_station(station_id: int, db: Session = Depends(get_db)):
    db.delete(_get(db, Station, station_id))
    _commit(db, "station")


# Antennas -------------------------------------------------------------------
@router.get("/antennas", response_model=list[AntennaOut])
def list_antennas(q: str = "", db: Session = Depends(get_db)):
    stmt = select(Antenna).order_by(Antenna.name)
    if q:
        stmt = stmt.where(Antenna.name.ilike(f"%{q}%"))
    return list(db.scalars(stmt))


@router.post("/antennas", response_model=AntennaOut, status_code=201)
async def create_antenna(
    name: str = Form(..., min_length=1, max_length=120),
    category: str = Form("custom"),
    rx_gain_dbd: float = Form(0.0),
    description: str = Form(""),
    file: UploadFile = File(..., description=".ant pattern file"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not (file.filename or "").lower().endswith(".ant"):
        raise HTTPException(422, "Antenna pattern must be an .ant file")
    content = await file.read()
    if len(content) > 1_000_000:
        raise HTTPException(413, "Pattern file too large")
    if db.scalar(select(func.count()).where(Antenna.name == name)):
        raise HTTPException(409, "An antenna with that name already exists")
    try:
        category, pattern = safe_name(category), safe_name(name)
        save_antenna_pattern(settings.antenna_dir, category, pattern, content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, f"Invalid .ant file: {exc}") from exc
    antenna = Antenna(name=name, category=category, pattern=pattern,
                      rx_gain_dbd=rx_gain_dbd, description=description)
    db.add(antenna)
    _commit(db, "antenna")
    return antenna


@router.patch("/antennas/{antenna_id}", response_model=AntennaOut)
def update_antenna(antenna_id: int, body: AntennaUpdate, db: Session = Depends(get_db)):
    antenna = _get(db, Antenna, antenna_id)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(antenna, k, v)
    _commit(db, "antenna")
    return antenna


@router.delete("/antennas/{antenna_id}", status_code=204)
def delete_antenna(antenna_id: int, db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)):
    antenna = _get(db, Antenna, antenna_id)
    category, pattern = antenna.category, antenna.pattern
    db.delete(antenna)
    _commit(db, "antenna")
    shared = db.scalar(select(func.count()).where(Antenna.category == category,
                                                  Antenna.pattern == pattern))
    if not shared:
        remove_antenna_pattern(settings.antenna_dir, category, pattern)


# Plots ----------------------------------------------------------------------
def _plot_out(db: Session, plot: Plot) -> PlotOut:
    out = PlotOut.model_validate(plot)
    latest = db.scalar(select(PlotRun).where(PlotRun.plot_id == plot.id).order_by(PlotRun.id.desc()).limit(1))
    out.latest_run = run_summary(latest) if latest else None
    return out


def _validate_refs(db: Session, body: PlotIn) -> None:
    _get(db, Antenna, body.antenna_id)
    _get(db, Station, body.tx_station_id)
    if body.rx_station_id is not None:
        _get(db, Station, body.rx_station_id)


@router.get("/plots", response_model=list[PlotOut])
def list_plots(q: str = "", db: Session = Depends(get_db)):
    stmt = select(Plot).order_by(Plot.name)
    if q:
        stmt = stmt.where(Plot.name.ilike(f"%{q}%"))
    return [_plot_out(db, p) for p in db.scalars(stmt).unique()]


@router.post("/plots", response_model=PlotOut, status_code=201)
def create_plot(body: PlotIn, db: Session = Depends(get_db)):
    _validate_refs(db, body)
    plot = Plot(**body.model_dump())
    db.add(plot)
    _commit(db, "plot")
    db.refresh(plot)
    return _plot_out(db, plot)


@router.get("/plots/{plot_id}", response_model=PlotOut)
def get_plot(plot_id: int, db: Session = Depends(get_db)):
    return _plot_out(db, _get(db, Plot, plot_id))


@router.put("/plots/{plot_id}", response_model=PlotOut)
def update_plot(plot_id: int, body: PlotIn, db: Session = Depends(get_db)):
    plot = _get(db, Plot, plot_id)
    _validate_refs(db, body)
    for k, v in body.model_dump().items():
        setattr(plot, k, v)
    _commit(db, "plot")
    db.refresh(plot)
    return _plot_out(db, plot)


@router.delete("/plots/{plot_id}", status_code=204)
def delete_plot(plot_id: int, db: Session = Depends(get_db),
                settings: Settings = Depends(get_settings)):
    import shutil

    plot = _get(db, Plot, plot_id)
    run_ids = [r.id for r in plot.runs]
    db.delete(plot)
    _commit(db, "plot")
    for rid in run_ids:
        shutil.rmtree(settings.runs_dir / str(rid), ignore_errors=True)
