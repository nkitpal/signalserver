"""Antenna library: import (MSI/Planet, NSMA .adf, Radio Mobile .ant), generate, preview."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..engine.antennas import Pattern, PatternError, from_az_el, generate, parse_any, to_az_el
from ..engine.patterns import remove_antenna_pattern, safe_name, save_antenna_files
from ..models import Antenna
from ..schemas import AntennaOut, AntennaUpdate, GenerateIn
from .resources import _commit, _get

router = APIRouter(prefix="/api/antennas")

MAX_BYTES = 2_000_000
EXTENSIONS = (".ant", ".msi", ".pln", ".adf", ".txt")


async def _read_upload(file: UploadFile) -> bytes:
    name = (file.filename or "").lower()
    if not name.endswith(EXTENSIONS):
        raise HTTPException(422, "Upload an antenna pattern: .msi, .pln, .txt (MSI/Planet), .adf (NSMA) or .ant (Radio Mobile)")
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Pattern file too large")
    return data


def _parse(data: bytes, filename: str, flip_vertical: bool) -> Pattern:
    try:
        pattern = parse_any(data, filename)
    except PatternError as exc:
        raise HTTPException(422, f"Could not read antenna pattern: {exc}") from exc
    return pattern.flipped() if flip_vertical else pattern


def _apply(antenna: Antenna, p: Pattern) -> None:
    m = p.metrics()
    antenna.h_beamwidth = m["h_beamwidth"]
    antenna.v_beamwidth = m["v_beamwidth"]
    antenna.front_to_back = m["front_to_back"]
    antenna.electrical_tilt = p.electrical_tilt if p.electrical_tilt is not None else m["peak_tilt"]
    antenna.source_format = p.source_format
    antenna.pattern_data = {"horizontal": [round(float(v), 2) for v in p.horizontal],
                            "vertical": [round(float(v), 2) for v in p.vertical],
                            "vertical_complete": p.vertical_complete}


def _unique_name(db: Session, name: str) -> None:
    if db.scalar(select(func.count()).where(Antenna.name == name)):
        raise HTTPException(409, "An antenna with that name already exists")


@router.get("", response_model=list[AntennaOut])
def list_antennas(q: str = "", db: Session = Depends(get_db)):
    stmt = select(Antenna).order_by(Antenna.name)
    if q:
        stmt = stmt.where(Antenna.name.ilike(f"%{q}%"))
    return list(db.scalars(stmt))


@router.post("/preview")
async def preview_file(file: UploadFile = File(...), flip_vertical: bool = Form(False)):
    """Parse an uploaded pattern without saving it (for the import dialog)."""
    data = await _read_upload(file)
    return _parse(data, file.filename or "", flip_vertical).to_dict()


@router.post("", response_model=AntennaOut, status_code=201)
async def create_antenna(
    file: UploadFile = File(..., description="MSI/Planet, NSMA .adf or Radio Mobile .ant"),
    name: str = Form("", max_length=120),
    category: str = Form("custom"),
    rx_gain_dbd: float = Form(0.0),
    description: str = Form(""),
    manufacturer: str = Form(""),
    gain_dbi: float | None = Form(None, description="overrides the gain read from the file"),
    flip_vertical: bool = Form(False),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    data = await _read_upload(file)
    pattern = _parse(data, file.filename or "", flip_vertical)
    name = (name or pattern.name or "Antenna").strip()[:120]
    _unique_name(db, name)
    category, base = safe_name(category or "custom"), safe_name(name)
    az, el = to_az_el(pattern)
    ext = "." + (file.filename or "x.ant").lower().rsplit(".", 1)[-1]
    save_antenna_files(settings.antenna_dir, category, base, az, el, data, ext)
    antenna = Antenna(
        name=name, category=category, pattern=base, rx_gain_dbd=rx_gain_dbd, description=description,
        manufacturer=(manufacturer or pattern.manufacturer)[:120],
        gain_dbi=gain_dbi if gain_dbi is not None else pattern.gain_dbi,
        frequency_mhz=pattern.frequency_mhz,
    )
    _apply(antenna, pattern)
    db.add(antenna)
    _commit(db, "antenna")
    return antenna


@router.post("/generate")
def generate_antenna(body: GenerateIn, preview: bool = False, db: Session = Depends(get_db),
                     settings: Settings = Depends(get_settings)):
    """Make an omni or sector pattern from beamwidths; ``preview=true`` only returns it."""
    try:
        pattern = generate(body.kind, body.h_beamwidth, body.v_beamwidth, body.gain_dbi,
                           body.front_to_back, body.electrical_tilt, body.side_lobe, body.name)
    except PatternError as exc:
        raise HTTPException(422, str(exc)) from exc
    if preview:
        return pattern.to_dict()
    if body.kind == "omni":
        default = f"Omni {pattern.gain_dbi:g} dBi"
    else:
        default = f"Sector {body.h_beamwidth:g}° {pattern.gain_dbi:g} dBi"
    name = (body.name or default).strip()[:120]
    _unique_name(db, name)
    category, base = safe_name(body.category or "generated"), safe_name(name)
    az, el = to_az_el(pattern)
    save_antenna_files(settings.antenna_dir, category, base, az, el)
    antenna = Antenna(name=name, category=category, pattern=base, gain_dbi=pattern.gain_dbi,
                      frequency_mhz=body.frequency_mhz, description=body.description)
    _apply(antenna, pattern)
    antenna.electrical_tilt = body.electrical_tilt
    db.add(antenna)
    _commit(db, "antenna")
    return AntennaOut.model_validate(antenna).model_dump(mode="json")


@router.get("/{antenna_id}/pattern")
def antenna_pattern(antenna_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    antenna = _get(db, Antenna, antenna_id)
    if antenna.pattern_data:
        out = dict(antenna.pattern_data)
    else:  # saved before patterns were stored: rebuild from the Signal-Server files
        base = settings.antenna_dir / antenna.category / antenna.pattern
        try:
            p = from_az_el(base.with_suffix(".az").read_text(), base.with_suffix(".el").read_text())
        except (OSError, ValueError, IndexError) as exc:
            raise HTTPException(404, "Pattern files missing") from exc
        out = {"horizontal": [round(float(v), 2) for v in p.horizontal],
               "vertical": [round(float(v), 2) for v in p.vertical], "vertical_complete": False}
    return {**out, "id": antenna.id, "name": antenna.name, "gain_dbi": antenna.gain_dbi,
            "h_beamwidth": antenna.h_beamwidth, "v_beamwidth": antenna.v_beamwidth,
            "electrical_tilt": antenna.electrical_tilt, "front_to_back": antenna.front_to_back,
            "source_format": antenna.source_format}


@router.patch("/{antenna_id}", response_model=AntennaOut)
def update_antenna(antenna_id: int, body: AntennaUpdate, db: Session = Depends(get_db)):
    antenna = _get(db, Antenna, antenna_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        if k == "name" and v is None:
            continue
        setattr(antenna, k, v)
    _commit(db, "antenna")
    return antenna


@router.delete("/{antenna_id}", status_code=204)
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
