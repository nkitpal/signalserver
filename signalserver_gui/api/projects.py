"""Projects, folder trees, imported KML layers and project export."""

from __future__ import annotations

import io
import shutil
import uuid
import zipfile

import simplekml
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import box
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..layers import LayerError, kml_bytes, parse_kml
from ..models import Folder, Layer, Plot, PlotRun, Project, Station
from ..schemas import FolderIn, FolderOut, LayerOut, MoveIn, ProjectIn, ProjectOut
from .resources import _commit, _get, _plot_out, _station_out, resolve_scope

router = APIRouter(prefix="/api")


# Projects ------------------------------------------------------------------------
@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return list(db.scalars(select(Project).order_by(Project.name)))


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectIn, db: Session = Depends(get_db)):
    project = Project(**body.model_dump())
    db.add(project)
    _commit(db, "project")
    return project


@router.put("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: int, body: ProjectIn, db: Session = Depends(get_db)):
    project = _get(db, Project, project_id)
    project.name, project.description = body.name, body.description
    _commit(db, "project")
    return project


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: int, db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)):
    project = _get(db, Project, project_id)
    run_ids = list(db.scalars(select(PlotRun.id).join(Plot).where(Plot.project_id == project_id)))
    layer_files = list(db.scalars(select(Layer.filename).where(Layer.project_id == project_id)))
    db.delete(project)
    _commit(db, "project")
    for rid in run_ids:
        shutil.rmtree(settings.runs_dir / str(rid), ignore_errors=True)
    for name in layer_files:
        (settings.layers_dir / name).unlink(missing_ok=True)


@router.get("/projects/{project_id}/tree")
def project_tree(project_id: int, db: Session = Depends(get_db)):
    """Everything in a project as flat lists; items carry ``folder_id`` (null = project root)."""
    project = _get(db, Project, project_id)
    folders = db.scalars(select(Folder).where(Folder.project_id == project_id)
                         .order_by(Folder.position, Folder.name))
    stations = db.scalars(select(Station).where(Station.project_id == project_id).order_by(Station.name))
    plots = db.scalars(select(Plot).where(Plot.project_id == project_id).order_by(Plot.name)).unique()
    layers = db.scalars(select(Layer).where(Layer.project_id == project_id).order_by(Layer.name))
    return {
        "project": ProjectOut.model_validate(project).model_dump(mode="json"),
        "folders": [FolderOut.model_validate(f).model_dump(mode="json") for f in folders],
        "stations": [_station_out(s).model_dump(mode="json") for s in stations],
        "plots": [_plot_out(db, p).model_dump(mode="json") for p in plots],
        "layers": [_layer_out(layer).model_dump(mode="json") for layer in layers],
    }


# Folders ---------------------------------------------------------------------------
def _check_parent(db: Session, project_id: int, parent_id: int | None, folder_id: int | None = None):
    if parent_id is None:
        return
    parent = _get(db, Folder, parent_id)
    if parent.project_id != project_id:
        raise HTTPException(422, "Parent folder belongs to a different project")
    # Refuse cycles: walk up from the new parent.
    seen = set()
    node = parent
    while node is not None:
        if node.id == folder_id or node.id in seen:
            raise HTTPException(422, "A folder cannot be moved inside itself")
        seen.add(node.id)
        node = db.get(Folder, node.parent_id) if node.parent_id else None


@router.post("/projects/{project_id}/folders", response_model=FolderOut, status_code=201)
def create_folder(project_id: int, body: FolderIn, db: Session = Depends(get_db)):
    _get(db, Project, project_id)
    _check_parent(db, project_id, body.parent_id)
    folder = Folder(project_id=project_id, **body.model_dump())
    db.add(folder)
    _commit(db, "folder")
    return folder


@router.put("/folders/{folder_id}", response_model=FolderOut)
def update_folder(folder_id: int, body: FolderIn, db: Session = Depends(get_db)):
    folder = _get(db, Folder, folder_id)
    _check_parent(db, folder.project_id, body.parent_id, folder.id)
    folder.name, folder.parent_id, folder.position = body.name, body.parent_id, body.position
    _commit(db, "folder")
    return folder


@router.delete("/folders/{folder_id}", status_code=204)
def delete_folder(folder_id: int, db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings)):
    """Delete a folder with everything inside it (sub-folders, stations, plots, layers)."""
    folder = _get(db, Folder, folder_id)
    ids = _descendant_ids(db, folder)
    run_ids = list(db.scalars(select(PlotRun.id).join(Plot).where(Plot.folder_id.in_(ids))))
    layer_files = list(db.scalars(select(Layer.filename).where(Layer.folder_id.in_(ids))))
    db.delete(folder)
    try:
        db.commit()
    except Exception as exc:  # a plot outside the folder still uses a station inside it
        db.rollback()
        raise HTTPException(409, "A plot outside this folder uses a station inside it") from exc
    for rid in run_ids:
        shutil.rmtree(settings.runs_dir / str(rid), ignore_errors=True)
    for name in layer_files:
        (settings.layers_dir / name).unlink(missing_ok=True)


def _descendant_ids(db: Session, folder: Folder) -> list[int]:
    ids, frontier = [folder.id], [folder.id]
    while frontier:
        frontier = list(db.scalars(select(Folder.id).where(Folder.parent_id.in_(frontier))))
        ids += frontier
    return ids


@router.patch("/{kind}/{item_id}/move", status_code=204)
def move_item(kind: str, item_id: int, body: MoveIn, db: Session = Depends(get_db)):
    """Move a station, plot or layer into another folder of the same project (null = root)."""
    model = {"stations": Station, "plots": Plot, "layers": Layer}.get(kind)
    if model is None:
        raise HTTPException(404, "Unknown item type")
    item = _get(db, model, item_id)
    if body.folder_id is not None:
        folder = _get(db, Folder, body.folder_id)
        if folder.project_id != item.project_id:
            raise HTTPException(422, "Folder belongs to a different project")
    item.folder_id = body.folder_id
    _commit(db, kind[:-1])


# Imported layers ----------------------------------------------------------------------
def _layer_out(layer: Layer) -> LayerOut:
    out = LayerOut.model_validate(layer)
    out.url = f"/api/layers/{layer.id}/file"
    if layer.footprint is not None:
        out.bounds = list(to_shape(layer.footprint).bounds)
    return out


@router.post("/projects/{project_id}/layers", status_code=201)
async def import_layer(
    project_id: int,
    file: UploadFile = File(..., description=".kml or .kmz"),
    folder_id: int | None = Form(None),
    name: str = Form(""),
    create_stations: bool = Form(False),
    station_height_m: float = Form(10.0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Import a KML/KMZ as a layer; optionally turn its placemark points into stations."""
    project_id, folder_id = resolve_scope(db, project_id, folder_id)
    filename = file.filename or "layer.kml"
    ext = ".kmz" if filename.lower().endswith(".kmz") else ".kml"
    if not filename.lower().endswith((".kml", ".kmz")):
        raise HTTPException(422, "Upload a .kml or .kmz file")
    data = await file.read()
    try:
        parsed = parse_kml(kml_bytes(data, filename))
    except LayerError as exc:
        raise HTTPException(422, str(exc)) from exc
    stored = f"{uuid.uuid4().hex}{ext}"
    settings.layers_dir.mkdir(parents=True, exist_ok=True)
    (settings.layers_dir / stored).write_bytes(data)
    layer = Layer(
        project_id=project_id, folder_id=folder_id,
        name=(name or parsed.name or filename.rsplit(".", 1)[0])[:160], kind=ext[1:],
        filename=stored, feature_count=parsed.feature_count,
        footprint=from_shape(box(*parsed.bounds), srid=4326) if parsed.bounds else None,
    )
    db.add(layer)
    created, skipped = [], []
    if create_stations:
        existing = set(db.scalars(select(Station.name).where(Station.project_id == project_id)))
        for pt in parsed.points:
            if not -70 <= pt["latitude"] <= 70 or pt["name"] in existing:
                skipped.append(pt["name"])
                continue
            st = Station(project_id=project_id, folder_id=folder_id, name=pt["name"][:120],
                         height_m=station_height_m, notes=pt["description"])
            st.set_position(pt["latitude"], pt["longitude"])
            db.add(st)
            existing.add(pt["name"])
            created.append(pt["name"])
    try:
        _commit(db, "layer")
    except HTTPException:
        (settings.layers_dir / stored).unlink(missing_ok=True)
        raise
    return {"layer": _layer_out(layer).model_dump(mode="json"), "stations_created": created,
            "stations_skipped": skipped, "points": len(parsed.points)}


@router.get("/layers/{layer_id}/file")
def layer_file(layer_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    layer = _get(db, Layer, layer_id)
    path = settings.layers_dir / layer.filename
    if not path.is_file():
        raise HTTPException(404, "Layer file missing")
    media = "application/vnd.google-earth.kmz" if layer.kind == "kmz" else "application/vnd.google-earth.kml+xml"
    return FileResponse(path, media_type=media, filename=f"{layer.name}.{layer.kind}")


@router.delete("/layers/{layer_id}", status_code=204)
def delete_layer(layer_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    layer = _get(db, Layer, layer_id)
    name = layer.filename
    db.delete(layer)
    _commit(db, "layer")
    (settings.layers_dir / name).unlink(missing_ok=True)


# Export -----------------------------------------------------------------------------
@router.get("/projects/{project_id}/export.kmz")
def export_project(project_id: int, db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)):
    """One KMZ with the project's folder tree, stations and latest coverage of every plot.

    Open it in Google Earth (desktop or earth.google.com: Projects > Open > Import KML file).
    """
    project = _get(db, Project, project_id)
    kml = simplekml.Kml(name=project.name)
    folders = list(db.scalars(select(Folder).where(Folder.project_id == project_id)))
    kfolders: dict[int | None, simplekml.Folder] = {None: kml.document}

    def kfolder(fid):
        if fid not in kfolders:
            f = next(f for f in folders if f.id == fid)
            kfolders[fid] = kfolder(f.parent_id).newfolder(name=f.name)
        return kfolders[fid]

    for st in db.scalars(select(Station).where(Station.project_id == project_id).order_by(Station.name)):
        pt = kfolder(st.folder_id).newpoint(name=st.name, coords=[(st.longitude, st.latitude, st.height_m)])
        pt.altitudemode = simplekml.AltitudeMode.relativetoground
        pt.extrude = 1
        pt.description = st.notes or ""
    images: list[tuple] = []
    for plot in db.scalars(select(Plot).where(Plot.project_id == project_id).order_by(Plot.name)).unique():
        run = db.scalar(select(PlotRun).where(PlotRun.plot_id == plot.id, PlotRun.north.is_not(None))
                        .order_by(PlotRun.id.desc()).limit(1))
        png = settings.runs_dir / str(run.id) / "coverage.png" if run else None
        if png is None or not png.exists():
            continue
        arcname = f"files/plot{plot.id}_run{run.id}.png"
        ov = kfolder(plot.folder_id).newgroundoverlay(name=plot.name)
        ov.icon.href = arcname
        ov.latlonbox.north, ov.latlonbox.south = run.north, run.south
        ov.latlonbox.east, ov.latlonbox.west = run.east, run.west
        ov.color = simplekml.Color.changealphaint(int(round(plot.opacity * 255)), simplekml.Color.white)
        images.append((png, arcname))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", kml.kml())
        for path, arcname in images:
            zf.write(path, arcname)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in project.name) or "project"
    return Response(buf.getvalue(), media_type="application/vnd.google-earth.kmz",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.kmz"'})
