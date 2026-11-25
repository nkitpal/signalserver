"""Estimate where a transmitter is from received-power measurements.

Two methods, both scoring every cell of a grid around the measurements:

* ``terrain`` (default): candidate transmitter positions on a grid around the
  measurements; for each, Signal-Server predicts the path loss L_i to every
  measurement point (one coverage run per candidate, run in parallel). The
  EIRP is estimated as mean_i(P_i + L_i) and the score is the RMS of
  P_i + L_i - EIRP. The grid is then refined around the three best
  candidates. Signal-Server's ITM is not reciprocal over rough terrain (a
  valley-to-peak path can differ by 50 dB from peak-to-valley), so the
  prediction always runs from the candidate transmitter outwards. Cells
  within +3 dB of the best RMS form the likely area.
* ``logdistance``: no terrain; fits P_i = A - 10 n log10(d_i) per cell
  (n clamped to 1.5..6) and scores the RMS residual. Fast, for flat areas.

Assumes an omnidirectional transmitter and isotropic receive antennas.
"""

from __future__ import annotations

import json
import math
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import numpy as np
from PIL import Image
from sqlalchemy.orm import Session

from .config import Settings
from .elevation.provider import ElevationProvider
from .elevation.tiles import coverage_bbox, tiles_for_bbox
from .engine import outputs
from .engine.command import build_args
from .engine.runner import RunError, _exec
from .engine.values import NODATA_INDEX, QUANTITIES, decode_indices, write_measurement_palette
from .models import Localization

ProgressFn = Callable[[float, str], None]
AREA_MARGIN_DB = 3.0
MAX_PIXELS = 4_000_000
COARSE_N = 13  # candidate grid per side
FINE_N = 5  # refinement grid per side, around each of the 3 best cells


def loc_dir(settings: Settings, loc_id: int) -> Path:
    return settings.data_dir / "localize" / str(loc_id)


def _engine_params(loc: Localization, lat: float, lon: float, radius_km: float, res: int) -> dict:
    return {
        "name": f"loc{loc.id}", "frequency_mhz": loc.frequency_mhz, "erp_watts": 0.0,
        "radius_km": radius_km, "resolution": res, "rx_height_m": loc.rx_height_m,
        "rx_threshold": None, "propagation_model": loc.propagation_model,
        "propagation_mode": None, "terrain_code": None, "terrain_dielectric": None,
        "terrain_conductivity": None, "climate_code": None, "reliability": None, "confidence": None,
        "ground_clutter_m": None, "knife_edge": False, "use_dbm": False, "metric_units": True,
        "antenna": {"name": "isotropic", "category": "_none", "pattern": "_none", "rx_gain_dbd": 0.0},
        "tx": {"name": "candidate", "latitude": lat, "longitude": lon, "height_m": loc.tx_height_m,
               "polarization": "vertical", "rotation_deg": 0.0, "downtilt_deg": 0.0,
               "downtilt_direction_deg": 0.0},
    }


def _km(lat1, lon1, lat2, lon2):
    return math.hypot((lat2 - lat1) * 111.2, (lon2 - lon1) * 111.2 * math.cos(math.radians((lat1 + lat2) / 2)))


def _predict(loc, ms, lat, lon, res, key, work, sdf, settings):
    """Path loss (dB, NaN if not reached) from a candidate transmitter to each measurement."""
    import uuid

    q = QUANTITIES["loss"]
    run = work / f"{key}_{uuid.uuid4().hex[:8]}"  # unique: candidates can coincide
    run.mkdir(parents=True, exist_ok=True)
    reach = max(_km(lat, lon, m["latitude"], m["longitude"]) for m in ms) + 0.5
    palette = write_measurement_palette(q, run)
    args = build_args(_engine_params(loc, lat, lon, reach, res), sdf_dir=sdf,
                      antenna_dir=settings.antenna_dir.resolve(), palette=Path(palette),
                      output_base=Path("lossmap"))
    try:
        out = _exec([settings.engine_binary(None), "-nothreads", *args], run, settings.engine_timeout_s)
        bounds = outputs.parse_bounds(out)
        if bounds is None:
            return np.full(len(ms), np.nan)
        idx = decode_indices(outputs.read_rgb(run / "lossmap.ppm"))
    finally:
        shutil.rmtree(run, ignore_errors=True)
    loss = (q.top + idx.astype(np.float32) * q.step).astype(np.float32)
    loss[idx == NODATA_INDEX] = np.nan
    lats = np.array([m["latitude"] for m in ms])
    lons = np.array([m["longitude"] for m in ms])
    return _sample(bounds, loss, lats, lons)


def _sample(bounds, arr, lats, lons, window: int = 1):
    """Value at each point: median over a (2*window+1)^2 pixel neighbourhood.

    Over rough terrain loss changes by 10+ dB between neighbouring cells (shadow
    edges), so a single pixel makes the fit chase noise; the median is robust.
    """
    north, east, south, west = bounds
    h, w = arr.shape
    rows = np.floor((north - lats) / (north - south) * h).astype(int)
    cols = np.floor((lons - west) / (east - west) * w).astype(int)
    out = np.full(lats.shape, np.nan, dtype=np.float32)
    for k, (r, c) in enumerate(zip(rows, cols)):
        if 0 <= r < h and 0 <= c < w:
            patch = arr[max(r - window, 0) : r + window + 1, max(c - window, 0) : c + window + 1]
            if np.isfinite(patch).any():
                out[k] = np.nanmedian(patch)
    return out


def _grid(west, south, east, north, dpp):
    w = max(2, int(math.ceil((east - west) / dpp)))
    h = max(2, int(math.ceil((north - south) / dpp)))
    scale = math.sqrt(w * h / MAX_PIXELS)
    if scale > 1:
        w, h = int(w / scale), int(h / scale)
    lons = west + (np.arange(w) + 0.5) * (east - west) / w
    lats = north - (np.arange(h) + 0.5) * (north - south) / h
    return np.meshgrid(lats, lons, indexing="ij")


def _search_box(loc, ms):
    lats = [m["latitude"] for m in ms]
    lons = [m["longitude"] for m in ms]
    lat0 = sum(lats) / len(lats)
    dlat = loc.radius_km / 111.2
    dlon = dlat / max(math.cos(math.radians(lat0)), 0.05)
    return min(lons) - dlon, min(lats) - dlat, max(lons) + dlon, max(lats) + dlat


def _grid_axes(west, south, east, north, n):
    lons = west + (np.arange(n) + 0.5) * (east - west) / n
    lats = north - (np.arange(n) + 0.5) * (north - south) / n
    return lats, lons


def _score_terrain(loc, ms, settings, session, progress, work):
    from concurrent.futures import ThreadPoolExecutor

    west, south, east, north = box = _search_box(loc, ms)
    reach = _km(south, west, north, east) + 1
    tiles = set(tiles_for_bbox(*coverage_bbox((south + north) / 2, (west + east) / 2, reach)))
    sdf = ElevationProvider(settings, session, model=loc.elevation_model).ensure_tiles(
        tiles, lambda f, s: progress(0.05 + 0.15 * f, s)).resolve()
    p = np.array([m["dbm"] for m in ms], dtype=np.float32)
    workers = max(2, min(16, (os.cpu_count() or 2)))
    n = COARSE_N
    lats, lons = _grid_axes(west, south, east, north, n)
    cands = [(la, lo) for la in lats for lo in lons]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        done = 0
        coarse = []
        for L in pool.map(lambda c: _predict(loc, ms, c[0], c[1], 600, f"c{c[0]:.5f}_{c[1]:.5f}", work, sdf, settings), cands):
            coarse.append(L)
            done += 1
            if done % 10 == 0:
                progress(0.2 + 0.5 * done / len(cands), f"testing positions {done}/{len(cands)}")
        est = (p[:, None] + np.array(coarse).T).reshape(len(ms), n, n)
        rms, _ = _rms(est, len(ms))
        # Refine around the three best coarse cells.
        order = np.argsort(np.where(np.isfinite(rms), rms, np.inf), axis=None)[:3]
        dlat, dlon = (north - south) / n, (east - west) / n
        fine = []
        for k in order:
            r, c = divmod(int(k), n)
            for fy in np.linspace(-0.5, 0.5, FINE_N):
                for fx in np.linspace(-0.5, 0.5, FINE_N):
                    fine.append((lats[r] + fy * dlat, lons[c] + fx * dlon))
        run_at = lambda c: _predict(loc, ms, c[0], c[1], 1200, f"f{c[0]:.6f}_{c[1]:.6f}", work, sdf, settings)  # noqa: E731
        fine_est = []
        for i, L in enumerate(pool.map(run_at, fine)):
            fine_est.append(p + L)
            if i % 10 == 0:
                progress(0.7 + 0.2 * i / len(fine), f"refining {i}/{len(fine)}")
        # Second refinement: a finer grid around the best refined point.
        f_rms, _ = _rms(np.array(fine_est).T, len(ms))
        if np.isfinite(f_rms).any():
            bl, bo = fine[int(np.nanargmin(f_rms))]
            step = 1.0 / (FINE_N - 1)
            finer = [(bl + fy * dlat * step, bo + fx * dlon * step)
                     for fy in np.linspace(-1, 1, FINE_N) for fx in np.linspace(-1, 1, FINE_N)]
            progress(0.92, "final refinement")
            for L in pool.map(run_at, finer):
                fine_est.append(p + L)
            fine += finer
    return box, (lats, lons, est), (np.array(fine), np.array(fine_est).T)


def _rms(est, n_meas):
    valid = np.isfinite(est)
    count = valid.sum(0)
    need = n_meas if (count == n_meas).any() else 3
    ok = count >= need
    with np.errstate(invalid="ignore", divide="ignore"):
        eirp = np.nanmean(np.where(valid, est, np.nan), 0)
        rms = np.sqrt(np.nanmean(np.where(valid, (est - eirp) ** 2, np.nan), 0))
    return np.where(ok, rms, np.nan), np.where(ok, eirp, np.nan)


def _score_logdistance(loc, ms):
    lat0 = np.mean([m["latitude"] for m in ms])
    west, south, east, north = _search_box(loc, ms)
    lats, lons = _grid(west, south, east, north, (north - south) / 500)
    p = np.array([m["dbm"] for m in ms], dtype=np.float64)[:, None, None]
    dy = (lats[None] - np.array([m["latitude"] for m in ms])[:, None, None]) * 111.2
    dx = (lons[None] - np.array([m["longitude"] for m in ms])[:, None, None]) * 111.2 * math.cos(math.radians(lat0))
    x = 10 * np.log10(np.maximum(np.hypot(dx, dy), 0.01))  # km
    xm, pm = x.mean(0), p.mean(0)
    cov = ((x - xm) * (p - pm)).sum(0)
    var = ((x - xm) ** 2).sum(0)
    n = np.clip(-cov / np.maximum(var, 1e-9), 1.5, 6.0)
    # P = A - n x  ->  A = P + n x ; "EIRP" here is A (received power at 1 km).
    est = p + n[None] * x
    return (west, south, east, north), lats, lons, est.astype(np.float32), n


def run_localization(session: Session, settings: Settings, loc_id: int,
                     progress: ProgressFn | None = None) -> Localization:
    progress = progress or (lambda f, s: None)
    loc = session.get(Localization, loc_id)
    if loc is None:
        raise RunError("Localization no longer exists")
    ms = loc.measurements or []
    if len(ms) < 3:
        raise RunError("At least 3 measurements are needed")
    work = loc_dir(settings, loc.id)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    exponent = None
    extra_pts = extra_est = None
    if loc.method == "logdistance":
        (west, south, east, north), lats, lons, est, exponent = _score_logdistance(loc, ms)
    else:
        (west, south, east, north), (lat_ax, lon_ax, est), (extra_pts, extra_est) = _score_terrain(
            loc, ms, settings, session, progress, work)
        lats, lons = np.meshgrid(lat_ax, lon_ax, indexing="ij")
    rms, eirp = _rms(est, len(ms))
    if not np.isfinite(rms).any():
        raise RunError("No position is reached by enough measurements; increase the search radius")

    # Best position: grid cells plus refined points.
    flat = np.where(np.isfinite(rms), rms, np.inf)
    r, c = np.unravel_index(np.argmin(flat), rms.shape)
    best_lat, best_lon, best, e = float(lats[r, c]), float(lons[r, c]), float(rms[r, c]), float(eirp[r, c])
    pred = est[:, r, c]
    if extra_est is not None and len(extra_pts):
        f_rms, f_eirp = _rms(extra_est, len(ms))
        if np.isfinite(f_rms).any() and np.nanmin(f_rms) < best:
            k = int(np.nanargmin(f_rms))
            best_lat, best_lon = float(extra_pts[k][0]), float(extra_pts[k][1])
            best, e, pred = float(f_rms[k]), float(f_eirp[k]), extra_est[:, k]

    h, w = rms.shape
    likely = np.isfinite(rms) & (rms <= best + AREA_MARGIN_DB)
    cell_km2 = ((north - south) / h * 111.2) * ((east - west) / w * 111.2 * math.cos(math.radians(best_lat)))
    lat_l = np.append(lats[likely], best_lat)
    lon_l = np.append(lons[likely], best_lon)
    if extra_est is not None and len(extra_pts):
        f_rms, _ = _rms(extra_est, len(ms))
        near = np.isfinite(f_rms) & (f_rms <= best + AREA_MARGIN_DB)
        lat_l = np.append(lat_l, extra_pts[near, 0])
        lon_l = np.append(lon_l, extra_pts[near, 1])
    # Pad by half a grid cell: each tested point stands for its cell.
    pad_lat, pad_lon = (north - south) / h / 2, (east - west) / w / 2

    # Heatmap: green at the best RMS, red at best + 10 dB, transparent beyond.
    t = np.nan_to_num(np.clip((rms - best) / 10.0, 0, 1), nan=1.0)  # unscored cells are hidden anyway
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    show = np.isfinite(rms) & (rms <= best + 10)
    rgba[..., 0] = (255 * np.clip(2 * t, 0, 1)).astype(np.uint8)
    rgba[..., 1] = (255 * np.clip(2 - 2 * t, 0, 1)).astype(np.uint8)
    rgba[..., 2] = 40
    rgba[..., 3] = np.where(show, (230 - 150 * t).astype(np.uint8), 0)
    img = Image.fromarray(rgba, "RGBA")
    if max(h, w) < 400:  # coarse terrain grid: smooth it for display
        img = img.resize((w * 16, h * 16), Image.BILINEAR)
    img.save(work / "heatmap.png", optimize=True)

    result = {
        "latitude": round(best_lat, 6), "longitude": round(best_lon, 6), "rms_db": round(best, 2),
        "likely_area_km2": round(max(float(likely.sum()), 1.0) * cell_km2, 2),
        "likely_bounds": [float(lon_l.min() - pad_lon), float(lat_l.min() - pad_lat),
                          float(lon_l.max() + pad_lon), float(lat_l.max() + pad_lat)],
        "bounds": [west, south, east, north], "image": "heatmap.png",
        "residuals_db": [None if not np.isfinite(v) else round(float(v - e), 1) for v in pred],
        "candidates_tested": int(h * w + (0 if extra_pts is None else len(extra_pts))),
    }
    if loc.method == "logdistance":
        result.update(rssi_at_1km_dbm=round(e, 1), path_loss_exponent=round(float(exponent[r, c]), 2))
    else:
        result.update(eirp_dbm=round(e, 1), erp_w=round(10 ** ((e - 2.15 - 30) / 10), 4))
    loc.result = result
    (work / "result.json").write_text(json.dumps(result, indent=1))
    session.commit()
    progress(1.0, "done")
    return loc
