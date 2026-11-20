"""Antenna pattern and colour palette files used by Signal-Server."""

from __future__ import annotations

import re
import shutil
from importlib import resources
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str) -> str:
    cleaned = _SAFE.sub("_", name).strip("._")
    if not cleaned:
        raise ValueError("name must contain letters or digits")
    return cleaned[:100]


def db_to_ratio(db: float) -> float:
    return 10 ** (db / 20.0)


def ant_to_az_el(ant_text: str) -> tuple[str, str]:
    """Convert a 361+ line .ant pattern (dB) into Signal-Server .az/.el text.

    Lines 0-359 are the azimuth pattern; lines 360+ the elevation pattern from
    +90 to -90 degrees. Signal-Server's .el takes +10 to -90 degrees of
    downtilt, i.e. elevation-pattern lines 80..180.
    """
    values = [float(v) for v in ant_text.split() if v.strip()]
    if len(values) < 360 + 181:
        raise ValueError(f".ant file needs at least 541 values, got {len(values)}")
    az_lines = ["0.0"] + [f"{i:d}\t{db_to_ratio(values[i]):0.4f}" for i in range(360)]
    elev = values[360:]
    el_lines = ["0.0\t0.0"] + [
        f"{deg:d}\t{db_to_ratio(elev[80 + n]):0.4f}" for n, deg in enumerate(range(-10, 91))
    ]
    return "\n".join(az_lines) + "\n", "\n".join(el_lines) + "\n"


def install_bundled(antenna_dir: Path, color_dir: Path) -> list[tuple[str, str]]:
    """Copy bundled antenna patterns and palettes into the data dir.

    Returns ``(category, pattern)`` pairs of the bundled antennas.
    """
    root = resources.files("signalserver_gui.resources")
    found = []
    ant_root = root.joinpath("antennas")
    for cat in ant_root.iterdir():
        if not cat.is_dir():
            continue
        for f in cat.iterdir():
            dest = antenna_dir / cat.name / f.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                dest.write_bytes(f.read_bytes())
            if f.name.endswith(".ant"):
                found.append((cat.name, f.name[:-4]))
    color_dir.mkdir(parents=True, exist_ok=True)
    for f in root.joinpath("color_profiles").iterdir():
        dest = color_dir / f.name
        if not dest.exists():
            dest.write_bytes(f.read_bytes())
    return sorted(found)


def save_antenna_pattern(antenna_dir: Path, category: str, pattern: str, ant_bytes: bytes) -> Path:
    folder = antenna_dir / safe_name(category)
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / safe_name(pattern)
    text = ant_bytes.decode("utf-8", errors="strict")
    az, el = ant_to_az_el(text)
    base.with_suffix(".ant").write_text(text)
    base.with_suffix(".az").write_text(az)
    base.with_suffix(".el").write_text(el)
    return base


def remove_antenna_pattern(antenna_dir: Path, category: str, pattern: str) -> None:
    base = antenna_dir / safe_name(category) / safe_name(pattern)
    for ext in (".ant", ".az", ".el"):
        base.with_suffix(ext).unlink(missing_ok=True)


def palette_file(color_dir: Path, palette: str, use_dbm: bool, erp_watts: float) -> Path:
    """Pick the palette file matching the plot's output quantity."""
    ext = ".lcf" if erp_watts <= 0 else (".dcf" if use_dbm else ".scf")
    candidate = color_dir / f"{safe_name(palette)}{ext}"
    if not candidate.exists():
        candidate = color_dir / f"rainbow{ext}"
    return candidate


def read_palette(path: Path) -> list[dict]:
    """Parse ``value: r, g, b`` palette lines into legend entries."""
    entries = []
    for line in path.read_text().splitlines():
        line = line.split(";", 1)[0].strip()
        if ":" not in line:
            continue
        value, rgb = line.split(":", 1)
        try:
            r, g, b = (int(c) for c in rgb.split(","))
            entries.append({"value": float(value), "color": f"#{r:02x}{g:02x}{b:02x}"})
        except ValueError:
            continue
    return entries


def list_palettes(color_dir: Path) -> list[str]:
    return sorted({p.stem for p in color_dir.glob("*.?cf")})


def copy_tree_missing(src: Path, dest: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
