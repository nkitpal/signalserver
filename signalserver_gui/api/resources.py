"""CRUD endpoints for stations, antennas and plots."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..engine.patterns import remove_antenna_pattern, safe_name, save_antenna_pattern
from ..models import Antenna, Folder, Plot, PlotRun, Project, Station
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


# Project scoping ------------------------------------------------------------
def default_project(db: Session) -> Project:
    project = db.scalar(select(Project).where(Project.name == "Default"))
    if project is None:
        project = Project(name="Default", description="")
        db.add(project)
        db.flush()
    return project


def resolve_scope(db: Session, project_id: int | None, folder_id: int | None) -> tuple[int, int | None]:
    """Validate (project, folder) and return their ids; folder implies its project."""
    if folder_id is not None:
        folder = _get(db, Folder, folder_id)
        if project_id is not None and folder.project_id != project_id:
            raise HTTPException(422, "Folder belongs to a different project")
        return folder.project_id, folder.id
    if project_id is None:
        return default_project(db).id, None
    _get(db, Project, project_id)
    return project_id, None


# Stations -------------------------------------------------------------------
def _station_out(s: Station) -> StationOut:
    return StationOut(
        id=s.id, project_id=s.project_id, folder_id=s.folder_id, name=s.name, latitude=s.latitude, longitude=s.longitude, height_m=s.height_m,
        polarization=s.polarization, rotation_deg=s.rotation_deg, downtilt_deg=s.downtilt_deg,
        downtilt_direction_deg=s.downtilt_direction_deg, notes=s.notes,
        created_at=s.created_at, updated_at=s.updated_at,
    )


@router.get("/stations", response_model=list[StationOut])
def list_stations(q: str = "", project_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(Station).order_by(Station.name)
    if project_id is not None:
        stmt = stmt.where(Station.project_id == project_id)
    if q:
        stmt = stmt.where(Station.name.ilike(f"%{q}%"))
    return [_station_out(s) for s in db.scalars(stmt)]


@router.get("/stations.geojson")
def stations_geojson(project_id: int | None = None, db: Session = Depends(get_db)):
    feats = []
    stmt = select(Station).order_by(Station.name)
    if project_id is not None:
        stmt = stmt.where(Station.project_id == project_id)
    for s in db.scalars(stmt):
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
    data["project_id"], data["folder_id"] = resolve_scope(db, body.project_id, body.folder_id)
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
    project_id, folder_id = resolve_scope(db, body.project_id or station.project_id, body.folder_id)
    if project_id != station.project_id:
        raise HTTPException(422, "Stations cannot move between projects")
    data["project_id"], data["folder_id"] = project_id, folder_id
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


def _validate_refs(db: Session, body: PlotIn, project_id: int) -> None:
    _get(db, Antenna, body.antenna_id)
    stations = [_get(db, Station, body.tx_station_id)]
    if body.rx_station_id is not None:
        stations.append(_get(db, Station, body.rx_station_id))
    if any(st.project_id != project_id for st in stations):
        raise HTTPException(422, "Plot stations must belong to the plot's project")


@router.get("/plots", response_model=list[PlotOut])
def list_plots(q: str = "", project_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(Plot).order_by(Plot.name)
    if project_id is not None:
        stmt = stmt.where(Plot.project_id == project_id)
    if q:
        stmt = stmt.where(Plot.name.ilike(f"%{q}%"))
    return [_plot_out(db, p) for p in db.scalars(stmt).unique()]


@router.post("/plots", response_model=PlotOut, status_code=201)
def create_plot(body: PlotIn, db: Session = Depends(get_db)):
    data = body.model_dump()
    if body.project_id is None and body.folder_id is None:
        # Default to the transmitter's project.
        tx = _get(db, Station, body.tx_station_id)
        data["project_id"], data["folder_id"] = tx.project_id, None
    else:
        data["project_id"], data["folder_id"] = resolve_scope(db, body.project_id, body.folder_id)
    _validate_refs(db, body, data["project_id"])
    plot = Plot(**data)
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
    data = body.model_dump()
    project_id, folder_id = resolve_scope(db, body.project_id or plot.project_id, body.folder_id)
    if project_id != plot.project_id:
        raise HTTPException(422, "Plots cannot move between projects")
    data["project_id"], data["folder_id"] = project_id, folder_id
    _validate_refs(db, body, project_id)
    for k, v in data.items():
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
