"""Build Signal-Server command lines from a plot snapshot.

The snapshot (``plot_params``) is a plain dict stored on each run, so a run can
be reproduced and audited even after the plot or its stations are edited.
Values are stored metric; when ``metric_units`` is false Signal-Server reads
heights in feet and radius in miles, so they are converted here.
"""

from __future__ import annotations

from pathlib import Path

from ..models import Plot

M_TO_FT = 3.28084
KM_TO_MI = 0.621371


def plot_params(plot: Plot) -> dict:
    tx = plot.tx_station
    rx = plot.rx_station
    snap = {
        "plot_id": plot.id,
        "name": plot.name,
        "frequency_mhz": plot.frequency_mhz,
        "erp_watts": plot.erp_watts,
        "radius_km": plot.radius_km,
        "resolution": plot.resolution,
        "rx_height_m": plot.rx_height_m,
        "rx_threshold": plot.rx_threshold,
        "propagation_model": plot.propagation_model,
        "propagation_mode": plot.propagation_mode,
        "terrain_code": plot.terrain_code,
        "terrain_dielectric": plot.terrain_dielectric,
        "terrain_conductivity": plot.terrain_conductivity,
        "climate_code": plot.climate_code,
        "reliability": plot.reliability,
        "confidence": plot.confidence,
        "ground_clutter_m": plot.ground_clutter_m,
        "knife_edge": plot.knife_edge,
        "use_dbm": plot.use_dbm,
        "metric_units": plot.metric_units,
        "do_p2p_analysis": plot.do_p2p_analysis and rx is not None,
        "color_palette": plot.color_palette,
        "opacity": plot.opacity,
        "antenna": {
            "name": plot.antenna.name,
            "category": plot.antenna.category,
            "pattern": plot.antenna.pattern,
            "rx_gain_dbd": plot.antenna.rx_gain_dbd,
        },
        "tx": _station(tx),
        "rx": _station(rx) if rx is not None else None,
    }
    return snap


def _station(s) -> dict:
    return {
        "name": s.name,
        "latitude": round(s.latitude, 6),
        "longitude": round(s.longitude, 6),
        "height_m": s.height_m,
        "polarization": s.polarization,
        "rotation_deg": s.rotation_deg,
        "downtilt_deg": s.downtilt_deg,
        "downtilt_direction_deg": s.downtilt_direction_deg,
    }


def _fmt(v: float) -> str:
    return f"{v:.6f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def build_args(
    p: dict,
    *,
    sdf_dir: Path,
    antenna_dir: Path,
    palette: Path | None,
    output_base: Path,
    p2p: bool = False,
) -> list[str]:
    metric = bool(p["metric_units"])
    length = (lambda m: m) if metric else (lambda m: m * M_TO_FT)
    radius = p["radius_km"] if metric else p["radius_km"] * KM_TO_MI
    tx = p["tx"]
    ant = p["antenna"]

    args = [
        "-sdf", f"{sdf_dir}/",
        "-lat", _fmt(tx["latitude"]),
        "-lon", _fmt(tx["longitude"]),
        "-txh", _fmt(length(tx["height_m"])),
        "-f", _fmt(p["frequency_mhz"]),
        "-erp", _fmt(p["erp_watts"]),
        "-R", _fmt(radius),
        "-res", str(p["resolution"]),
        "-pm", str(p["propagation_model"]),
    ]
    if metric:
        args.append("-m")
    optional = [
        ("-pe", p.get("propagation_mode")),
        ("-te", p.get("terrain_code")),
        ("-terdic", p.get("terrain_dielectric")),
        ("-tercon", p.get("terrain_conductivity")),
        ("-cl", p.get("climate_code")),
        ("-rel", p.get("reliability")),
        ("-conf", p.get("confidence")),
    ]
    for flag, value in optional:
        if value is not None:
            args += [flag, _fmt(value)]
    if p.get("ground_clutter_m"):
        args += ["-gc", _fmt(length(p["ground_clutter_m"]))]
    if p.get("knife_edge"):
        args.append("-ked")
    if p.get("use_dbm"):
        args.append("-dbm")
    if p.get("rx_threshold") is not None:
        args += ["-rt", _fmt(p["rx_threshold"])]

    pattern = antenna_dir / ant["category"] / ant["pattern"]
    if pattern.with_suffix(".az").exists():
        args += ["-ant", str(pattern)]
    if tx["polarization"] == "horizontal":
        args.append("-hp")
    if tx["rotation_deg"]:
        args += ["-rot", _fmt(tx["rotation_deg"])]
    if tx["downtilt_deg"]:
        args += ["-dt", _fmt(tx["downtilt_deg"])]
    if tx["downtilt_direction_deg"]:
        args += ["-dtdir", _fmt(tx["downtilt_direction_deg"])]
    if palette is not None:
        args += ["-color", str(palette)]

    if p2p:
        rx = p["rx"]
        args += [
            "-rla", _fmt(rx["latitude"]),
            "-rlo", _fmt(rx["longitude"]),
            "-rxh", _fmt(length(rx["height_m"])),
            "-ng",
        ]
        if ant.get("rx_gain_dbd"):
            args += ["-rxg", _fmt(ant["rx_gain_dbd"])]
    else:
        args += ["-rxh", _fmt(length(p["rx_height_m"]))]

    args += ["-o", str(output_base)]
    return args
