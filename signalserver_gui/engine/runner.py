"""Execute one plot run end to end."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import Settings
from ..elevation.provider import ElevationProvider
from ..elevation.tiles import coverage_bbox, tiles_for_bbox
from ..models import PlotRun
from . import outputs
from .command import build_args
from .patterns import palette_file, read_palette
from .report import parse_report, read_profile

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]


class RunError(RuntimeError):
    pass


def _exec(cmd: list[str], cwd: Path, timeout: int) -> str:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, check=False, text=True,
        )
    except FileNotFoundError as exc:
        raise RunError(f"Signal-Server binary not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RunError(f"Signal-Server timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise RunError(f"Signal-Server exited with code {proc.returncode}:\n{proc.stdout[-4000:]}")
    return proc.stdout


def execute_run(
    run: PlotRun, session: Session, settings: Settings, progress: ProgressFn | None = None
) -> PlotRun:
    progress = progress or (lambda f, s: None)
    p = run.params
    tx = p["tx"]
    run_dir = settings.runs_dir / str(run.id)
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    # 1. Elevation -------------------------------------------------------
    bbox = coverage_bbox(tx["latitude"], tx["longitude"], p["radius_km"])
    tiles = set(tiles_for_bbox(*bbox))
    if p.get("do_p2p_analysis") and p.get("rx"):
        rx = p["rx"]
        w, s = min(tx["longitude"], rx["longitude"]), min(tx["latitude"], rx["latitude"])
        e, n = max(tx["longitude"], rx["longitude"]), max(tx["latitude"], rx["latitude"])
        tiles |= set(tiles_for_bbox(w - 0.05, s - 0.05, e + 0.05, n + 0.05))
    provider = ElevationProvider(settings, session)
    sdf_dir = provider.ensure_tiles(tiles, lambda f, s: progress(0.05 + 0.35 * f, s)).resolve()

    # 2. Coverage --------------------------------------------------------
    progress(0.45, "computing coverage")
    palette = palette_file(settings.color_dir, p["color_palette"], p["use_dbm"], p["erp_watts"])
    base = run_dir / "coverage"
    binary = settings.engine_binary(p["resolution"])
    args = build_args(
        p, sdf_dir=sdf_dir, antenna_dir=settings.antenna_dir.resolve(),
        palette=palette.resolve() if palette.exists() else None,
        # Relative: the engine copies output names into 80 byte buffers.
        output_base=Path(base.name),
    )
    cmd = [binary, *args]
    run.command = cmd
    out = _exec(cmd, run_dir, settings.engine_timeout_s)
    bounds = outputs.parse_bounds(out)
    if bounds is None or not base.with_suffix(".ppm").exists():
        log.warning("Engine produced invalid bounds; retrying single-threaded")
        cmd = [binary, "-nothreads", *args]
        run.command = cmd
        out = _exec(cmd, run_dir, settings.engine_timeout_s)
        bounds = outputs.parse_bounds(out)
    log_parts = [out]
    if bounds is None:
        raise RunError("Signal-Server did not report valid plot boundaries.\n" + out[-3000:])

    progress(0.75, "writing outputs")
    rgba = outputs.ppm_to_rgba(base.with_suffix(".ppm"))
    outputs.write_png(rgba, run_dir / "coverage.png")
    outputs.write_cog(rgba, bounds, run_dir / "coverage.tif")
    base.with_suffix(".ppm").unlink(missing_ok=True)
    run.set_bounds(*bounds)
    run.legend = read_palette(palette) if palette.exists() else []

    # 3. Point-to-point analysis ----------------------------------------
    report = None
    if p.get("do_p2p_analysis") and p.get("rx"):
        progress(0.85, "path analysis")
        p2p_base = run_dir / "p2p_link"
        p2p_args = build_args(
            p, sdf_dir=sdf_dir, antenna_dir=settings.antenna_dir.resolve(), palette=None,
            output_base=Path(p2p_base.name), p2p=True,
        )
        out2 = _exec([binary, *p2p_args], run_dir, settings.engine_timeout_s)
        log_parts.append(out2)
        txt = p2p_base.with_suffix(".txt")
        if txt.exists():
            report = parse_report(txt.read_text())
            report["series"] = read_profile(p2p_base)
            report["units"] = {"distance": "km" if p["metric_units"] else "mi",
                               "height": "m" if p["metric_units"] else "ft"}
            (run_dir / "p2p_link.json").write_text(json.dumps(report, indent=1))
        p2p_base.with_suffix(".ppm").unlink(missing_ok=True)
    run.report = report

    # 4. Google Earth + bundle --------------------------------------------
    outputs.write_kmz(
        run_dir / "coverage.kmz", name=p["name"], image_name="files/coverage.png",
        image_path=run_dir / "coverage.png", bounds=bounds, opacity=p["opacity"],
        tx=tx, rx=p.get("rx") if p.get("do_p2p_analysis") else None, report=report,
    )
    (run_dir / "run.json").write_text(
        json.dumps({"params": p, "command": cmd, "bounds": bounds}, indent=1)
    )
    files = sorted(f.name for f in run_dir.iterdir() if f.is_file())
    with zipfile.ZipFile(run_dir / "bundle.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            zf.write(run_dir / name, name)
    run.files = sorted(f.name for f in run_dir.iterdir() if f.is_file())
    run.log = "\n".join(log_parts)[-20000:]
    run.finished_at = datetime.now(timezone.utc)
    progress(1.0, "done")
    return run
