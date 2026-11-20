"""Parse Signal-Server point-to-point output (``<base>.txt`` + profile files)."""

from __future__ import annotations

import re
from pathlib import Path

_NUM = r"([-+]?\d+(?:\.\d+)?)"

_FIELDS = {
    "distance": rf"Distance to Rx:\s*{_NUM}\s*(\w+)",
    "azimuth_to_rx_deg": rf"Azimuth to Rx:\s*{_NUM}",
    "azimuth_to_tx_deg": rf"Azimuth to Tx:\s*{_NUM}",
    "propagation_model": r"Propagation model:\s*(.+)",
    "model_subtype": r"Model sub-type:\s*(.+)",
    "free_space_path_loss_db": rf"Free space path loss:\s*{_NUM}",
    "computed_path_loss_db": rf"Computed path loss:\s*{_NUM}",
    "terrain_shielding_db": rf"Attenuation due to terrain shielding:\s*{_NUM}",
    "field_strength_dbuv_m": rf"Field strength at Rx:\s*{_NUM}",
    "signal_power_dbm": rf"Signal power level at Rx:\s*{_NUM}",
    "power_density_dbw_m2": rf"Signal power density at Rx:\s*{_NUM}",
    "model_error_number": rf"model error number:\s*{_NUM}",
    "rx_height_to_clear_obstructions": rf"must be raised to at least\s*{_NUM}\s*(\w+)\s*AGL\s*\nto clear all obstructions",
    "rx_height_to_clear_fresnel": rf"must be raised to at least\s*{_NUM}\s*(\w+)\s*AGL\s*\nto clear the first Fresnel zone",
    "rx_height_to_clear_fresnel60": rf"must be raised to at least\s*{_NUM}\s*(\w+)\s*AGL\s*\nto clear 60%",
}

_SITE = re.compile(
    rf"(Transmitter|Receiver) site:\s*(.*?)\nSite location:\s*{_NUM},\s*{_NUM}\n"
    rf"Ground elevation:\s*{_NUM}\s*(\w+) AMSL\nAntenna height:\s*{_NUM}\s*\w+ AGL / {_NUM}",
)
_OBSTRUCTION = re.compile(
    rf"^\s*{_NUM}\s*([NS]),\s*{_NUM}\s*([EW]),\s*{_NUM}\s*(\w+),\s*{_NUM}\s*(\w+) AMSL", re.M
)


def parse_report(text: str) -> dict:
    out: dict = {"raw": text}
    for site in _SITE.finditer(text):
        key = "tx" if site.group(1) == "Transmitter" else "rx"
        out[key] = {
            "name": site.group(2).strip(),
            "latitude": float(site.group(3)),
            "longitude": float(site.group(4)),
            "ground_elevation": float(site.group(5)),
            "units": site.group(6),
            "antenna_agl": float(site.group(7)),
            "antenna_amsl": float(site.group(8)),
        }
    for key, pattern in _FIELDS.items():
        m = re.search(pattern, text)
        if not m:
            continue
        try:
            out[key] = float(m.group(1))
        except ValueError:
            out[key] = m.group(1).strip()
        if key == "distance":
            out["distance_units"] = m.group(2)
    obstructions = []
    for m in _OBSTRUCTION.finditer(text):
        lat = float(m.group(1)) * (1 if m.group(2) == "N" else -1)
        lon = float(m.group(3)) * (1 if m.group(4) == "E" else -1)
        obstructions.append(
            {"latitude": lat, "longitude": lon, "distance": float(m.group(5)),
             "elevation": float(m.group(7))}
        )
    out["obstructions"] = obstructions
    out["line_of_sight_clear"] = "No obstructions to LOS path" in text or not obstructions
    return out


def read_profile(base: Path) -> dict:
    """Read the gnuplot-style series written next to a P2P report."""
    series = {}
    for name in ("profile", "reference", "curvature", "fresnel", "fresnel60"):
        path = Path(f"{base}_{name}")
        if not path.exists():
            continue
        pts = []
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                try:
                    pts.append([float(parts[0]), float(parts[1])])
                except ValueError:
                    continue
        series[name] = pts
    return series
