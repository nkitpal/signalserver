"""Plot runs (coverage results) and background jobs."""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from geoalchemy2.functions import ST_Intersects, ST_MakeEnvelope
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..engine.command import plot_params
from ..models import Job, JobStatus, Plot, PlotRun
from ..schemas import JobOut, RunDetail, RunSummary
from ..worker import enqueue

router = APIRouter(prefix="/api")

_MEDIA = {
    ".png": "image/png",
    ".tif": "image/tiff",
    ".kmz": "application/vnd.google-earth.kmz",
    ".zip": "application/zip",
    ".json": "application/json",
    ".txt": "text/plain",
}


def _status(run: PlotRun) -> tuple[str, str, float, str | None]:
    job = run.job
    if job is None:
        return ("done" if run.files else "failed"), "", 1.0, None
    status = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
    return status, job.stage, job.progress, job.error


def run_summary(run: PlotRun, detail: bool = False) -> RunSummary:
    status, stage, progress, error = _status(run)
    p = run.params or {}
    data = dict(
        id=run.id, plot_id=run.plot_id, plot_name=p.get("name"), status=status, stage=stage,
        progress=progress, error=error, north=run.north, south=run.south, east=run.east,
        west=run.west, created_at=run.created_at, finished_at=run.finished_at,
        files=run.files or [], legend=run.legend or [],
        image_url=f"/api/runs/{run.id}/files/coverage.png" if "coverage.png" in (run.files or []) else None,
        opacity=p.get("opacity", 0.7),
        units="dBm" if p.get("use_dbm") else ("dB" if p.get("erp_watts", 1) <= 0 else "dBuV/m"),
    )
    if detail:
        return RunDetail(**data, params=p, command=[str(c) for c in run.command or []],
                         log=run.log or "", report=run.report)
    return RunSummary(**data)


@router.post("/plots/{plot_id}/runs", response_model=RunSummary, status_code=202)
def start_run(plot_id: int, db: Session = Depends(get_db)):
    plot = db.get(Plot, plot_id)
    if plot is None:
        raise HTTPException(404, "Plot not found")
    run = PlotRun(plot_id=plot.id, params=plot_params(plot))
    db.add(run)
    db.flush()
    job = enqueue(db, "plot_run", {"run_id": run.id})
    run.job_id = job.id
    db.commit()
    db.refresh(run)
    return run_summary(run)


@router.get("/plots/{plot_id}/runs", response_model=list[RunSummary])
def plot_runs(plot_id: int, db: Session = Depends(get_db)):
    runs = db.scalars(select(PlotRun).where(PlotRun.plot_id == plot_id).order_by(PlotRun.id.desc()))
    return [run_summary(r) for r in runs]


@router.get("/runs", response_model=list[RunSummary])
def list_runs(
    west: float | None = None, south: float | None = None,
    east: float | None = None, north: float | None = None,
    limit: int = 100, db: Session = Depends(get_db),
):
    """Recent runs, optionally only those whose footprint intersects a bbox."""
    stmt = select(PlotRun).order_by(PlotRun.id.desc()).limit(min(limit, 500))
    if None not in (west, south, east, north):
        stmt = stmt.where(ST_Intersects(PlotRun.footprint, ST_MakeEnvelope(west, south, east, north, 4326)))
    return [run_summary(r) for r in db.scalars(stmt).unique()]


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(PlotRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run_summary(run, detail=True)


@router.delete("/runs/{run_id}", status_code=204)
def delete_run(run_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    run = db.get(PlotRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    if run.job is not None and run.job.status == JobStatus.running:
        raise HTTPException(409, "Run is still in progress")
    db.delete(run)
    db.commit()
    shutil.rmtree(settings.runs_dir / str(run_id), ignore_errors=True)


@router.get("/runs/{run_id}/files/{name}")
def run_file(run_id: int, name: str, download: bool = False, db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)):
    run = db.get(PlotRun, run_id)
    if run is None or name not in (run.files or []):
        raise HTTPException(404, "File not found")
    path = settings.runs_dir / str(run_id) / name
    if not path.is_file():
        raise HTTPException(404, "File missing on disk")
    suffix = path.suffix.lower()
    filename = f"{(run.params or {}).get('name', 'plot')}-run{run_id}-{name}"
    return FileResponse(
        path, media_type=_MEDIA.get(suffix, "application/octet-stream"),
        filename=filename if download else None,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    out = JobOut.model_validate(job)
    out.status = job.status.value
    return out
