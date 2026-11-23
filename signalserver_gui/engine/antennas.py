"""Antenna patterns: importers, a parametric generator and Signal-Server output.

Canonical representation (``Pattern``)
--------------------------------------
* ``horizontal[a]``: attenuation in dB (>= 0, 0 = strongest) at azimuth ``a``
  degrees clockwise from boresight, a = 0..359.
* ``vertical[d]``: attenuation in dB at angle ``d`` measured *downwards* from the
  front horizon: 0 = horizon in front, 90 = straight down, 180 = horizon behind,
  270 = straight up (the MSI/Planet convention).

Supported files
---------------
* MSI / Planet (``.msi``, ``.pln``, ``.txt``): ``KEY value`` header lines (the
  first line is the name even without ``NAME``), then ``HORIZONTAL n`` and
  ``VERTICAL n`` blocks of ``angle loss`` rows. Loss is non-negative dB; vertical
  angles follow the canonical convention. ``GAIN`` is dBd unless marked dBi.
* NSMA / TIA-EIA-804-B (``.adf``): ``KEY:,value`` headers (``GUNITS:,DBD/DBR``,
  ``MDGAIN``, ``ELTILT``...), then per cut ``PATCUT:,H|V``, ``NUPOIN``, ``FSTLST``
  and ``angle,value`` rows, ending ``ENDFIL``. Values are relative gain (dB, <= 0)
  for ``DBR`` patterns. The V-cut angle is read as an elevation (positive = above
  the horizon); vendors differ, so imports can be flipped after previewing.
* Radio Mobile ``.ant``: 360 horizontal values (V1); + 181 vertical values from
  +90 (up) to -90 (V2, also the format of Signal-Server's bundled patterns); or
  + 360 vertical values starting at +90 and decreasing (V3). Values are dB
  relative to the main lobe (normally <= 0).

Signal-Server reads ``.az`` (azimuth, linear field ratio) and ``.el`` (downtilt
-10..90 degrees, linear field ratio) files; the antenna *gain* is not in them and
must be part of the ERP.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

import numpy as np

DBI_PER_DBD = 2.15


class PatternError(ValueError):
    pass


@dataclass
class Pattern:
    horizontal: np.ndarray  # 360 attenuation values (dB)
    vertical: np.ndarray  # 360 attenuation values (dB), downward angles
    name: str = ""
    manufacturer: str = ""
    gain_dbi: float | None = None
    frequency_mhz: float | None = None
    polarization: str = ""
    electrical_tilt: float | None = None
    source_format: str = ""
    vertical_complete: bool = True
    warnings: list[str] = field(default_factory=list)

    def metrics(self) -> dict:
        h_bw = beamwidth(self.horizontal, 0)
        front = np.arange(-90, 91)  # downward angles in front of the antenna
        tilt = int(front[np.argmin(self.vertical[front % 360])])  # main-lobe downtilt (negative = up)
        v_bw = beamwidth(self.vertical, tilt % 360)
        return {
            "h_beamwidth": h_bw,
            "v_beamwidth": v_bw,
            "front_to_back": round(float(self.horizontal[180] - self.horizontal[0]), 1),
            "peak_tilt": float(tilt),  # degrees below the horizon
        }

    def to_dict(self) -> dict:
        d = asdict(self)
        d["horizontal"] = [round(float(v), 2) for v in self.horizontal]
        d["vertical"] = [round(float(v), 2) for v in self.vertical]
        d.update(self.metrics())
        return d

    def flipped(self) -> "Pattern":
        """Mirror the vertical cut top-to-bottom (for files using the other sign convention)."""
        idx = (-np.arange(360)) % 360
        p = Pattern(**{**self.__dict__, "vertical": self.vertical[idx].copy(), "warnings": list(self.warnings)})
        if p.electrical_tilt is not None:
            p.electrical_tilt = -p.electrical_tilt
        return p


# --- helpers -----------------------------------------------------------------
def _resample(angles: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Interpolate (angle, value) samples onto whole degrees 0..359 (circular)."""
    a = np.mod(np.asarray(angles, dtype=float), 360.0)
    v = np.asarray(values, dtype=float)
    order = np.argsort(a)
    a, v = a[order], v[order]
    a, keep = np.unique(a, return_index=True)
    v = v[keep]
    if len(a) < 4:
        raise PatternError("pattern needs at least 4 points")
    ext_a = np.concatenate([a - 360, a, a + 360])
    ext_v = np.concatenate([v, v, v])
    return np.interp(np.arange(360), ext_a, ext_v)


def _normalise(att: np.ndarray) -> np.ndarray:
    att = np.asarray(att, dtype=float)
    return np.clip(att - att.min(), 0, 100)


def beamwidth(att: np.ndarray, centre: int) -> float:
    """-3 dB beamwidth (degrees) around ``centre`` of a 360-point attenuation cut."""
    base = att[centre % 360]
    width = 0
    for side in (1, -1):
        k = 0
        while k < 180 and att[(centre + side * (k + 1)) % 360] - base <= 3.0:
            k += 1
        width += k
    return float(min(360, width + 1))


def _number(text: str) -> float | None:
    m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text or "")
    return float(m.group(0)) if m else None


def _gain_dbi(text: str, default_unit: str = "dbd") -> float | None:
    value = _number(text)
    if value is None:
        return None
    unit = "dbi" if re.search(r"dbi", text, re.I) else ("dbd" if re.search(r"dbd", text, re.I) else default_unit)
    return round(value + (DBI_PER_DBD if unit == "dbd" else 0.0), 2)


# --- MSI / Planet --------------------------------------------------------------
def parse_msi(text: str) -> Pattern:
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        raise PatternError("empty file")
    header: dict[str, str] = {}
    blocks: dict[str, list[tuple[float, float]]] = {}
    i = 0
    first = True
    while i < len(lines):
        key, _, value = lines[i].partition(" ")
        value = value.strip()
        ukey = key.upper()
        if ukey in ("HORIZONTAL", "VERTICAL"):
            n = int(_number(value) or 360)
            rows = []
            for row in lines[i + 1 : i + 1 + n]:
                parts = row.replace(",", " ").split()
                if len(parts) < 2:
                    raise PatternError(f"bad {ukey.lower()} row: {row!r}")
                rows.append((float(parts[0]), float(parts[1])))
            if len(rows) != n:
                raise PatternError(f"{ukey} declares {n} rows but has {len(rows)}")
            blocks[ukey] = rows
            i += 1 + n
            first = False
            continue
        if first and ukey != "NAME":
            header["NAME"] = lines[i]
        else:
            header[ukey] = value
        first = False
        i += 1
    if "HORIZONTAL" not in blocks:
        raise PatternError("no HORIZONTAL block")
    h = np.array(blocks["HORIZONTAL"])
    horizontal = _normalise(_resample(h[:, 0], h[:, 1]))
    warnings = []
    if "VERTICAL" in blocks:
        v = np.array(blocks["VERTICAL"])
        vertical = _normalise(_resample(v[:, 0], v[:, 1]))
        complete = True
    else:
        vertical = np.zeros(360)
        complete = False
        warnings.append("No vertical pattern in the file: elevation is treated as omnidirectional.")
    tilt = _number(header.get("TILT", "")) if re.search(r"elec", header.get("TILT", ""), re.I) else None
    if tilt is None and "ELECTRICAL_TILT" in header:
        tilt = _number(header["ELECTRICAL_TILT"])
    return Pattern(
        horizontal=horizontal, vertical=vertical, name=header.get("NAME", ""),
        manufacturer=header.get("MAKE", ""), gain_dbi=_gain_dbi(header.get("GAIN", ""), "dbd"),
        frequency_mhz=_number(header.get("FREQUENCY", "")), polarization=header.get("POLARIZATION", ""),
        electrical_tilt=tilt, source_format="msi", vertical_complete=complete, warnings=warnings,
    )


# --- NSMA / TIA-EIA-804-B ADF ----------------------------------------------------
def parse_adf(text: str) -> Pattern:
    header: dict[str, str] = {}
    cuts: dict[str, list[tuple[float, float]]] = {}
    current: list[tuple[float, float]] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^([A-Z0-9]{3,8}):,(.*)$", line, re.I)
        if m:
            key, value = m.group(1).upper(), m.group(2).strip().rstrip(",")
            if key == "PATCUT":
                cut = value.upper()[:1]
                if cut not in ("H", "V", "A", "E"):
                    raise PatternError(f"unknown PATCUT {value!r}")
                cut = "H" if cut in ("H", "A") else "V"
                if cut in cuts:  # several frequencies/polarisations: keep the first
                    current = None
                    continue
                current = cuts.setdefault(cut, [])
            elif key == "ENDFIL":
                break
            else:
                header.setdefault(key, value)
            continue
        if current is None:
            continue
        parts = [p for p in line.split(",") if p.strip()]
        if len(parts) >= 2:
            try:
                current.append((float(parts[0]), float(parts[1])))
            except ValueError as exc:
                raise PatternError(f"bad data row: {line!r}") from exc
    if "H" not in cuts or len(cuts["H"]) < 4:
        raise PatternError("no horizontal (PATCUT H) data")
    units = header.get("GUNITS", "DBD/DBR").upper().split("/")
    gain_unit = units[0] if units else "DBD"
    pattern_unit = units[1] if len(units) > 1 else "DBR"

    def to_att(rows):
        arr = np.array(rows)
        vals = arr[:, 1]
        att = (vals.max() - vals) if pattern_unit in ("DBR", "DBI", "DBD") else -vals
        return arr[:, 0], att

    a, att = to_att(cuts["H"])
    horizontal = _normalise(_resample(a, att))
    warnings = []
    if "V" in cuts and len(cuts["V"]) >= 4:
        a, att = to_att(cuts["V"])
        vertical = _normalise(_resample(-a, att))  # elevation (up positive) -> downward angle
        complete = True
    else:
        vertical = np.zeros(360)
        complete = False
        warnings.append("No vertical cut in the file: elevation is treated as omnidirectional.")
    gain = _number(header.get("MDGAIN", "")) or _number(header.get("HGGAIN", ""))
    gain_dbi = None if gain is None else round(gain + (DBI_PER_DBD if gain_unit == "DBD" else 0.0), 2)
    freq = _number(header.get("PATFRE", "")) or _number(header.get("HGHFRQ", ""))
    desc = " ".join(header.get(k, "") for k in ("DESCR1", "DESCR2")).strip()
    return Pattern(
        horizontal=horizontal, vertical=vertical, name=header.get("MODNUM", "") or desc,
        manufacturer=header.get("ANTMAN", ""), gain_dbi=gain_dbi, frequency_mhz=freq,
        polarization=header.get("POLARI", ""), electrical_tilt=_number(header.get("ELTILT", "")),
        source_format="adf", vertical_complete=complete, warnings=warnings,
    )


# --- Radio Mobile .ant -------------------------------------------------------------
def parse_radiomobile(text: str) -> Pattern:
    values = []
    for tok in text.split():
        try:
            values.append(float(tok))
        except ValueError as exc:
            raise PatternError(f"non-numeric value {tok!r} in .ant file") from exc
    n = len(values)
    if n < 360:
        raise PatternError(f".ant file has {n} values; at least 360 are needed")
    arr = np.array(values)
    # dB relative to the main lobe, normally <= 0; accept positive attenuation too.
    att = -arr if arr.max() <= 0.5 else arr
    horizontal = _normalise(att[:360])
    warnings: list[str] = []
    if n >= 720:
        k = np.arange(360)
        vertical = _normalise(_resample((k - 90) % 360, att[360:720]))  # line 361 = +90 (up), decreasing
        fmt, complete = "radiomobile_v3", True
    elif n >= 541:
        k = np.arange(181)
        known = att[360:541]
        down = k - 90  # +90 (up) .. -90 (down) -> downward angle -90..90
        # Back half is not in V2 files: mirror the front half.
        angles = np.concatenate([down, 180 - down[1:-1]])
        vals = np.concatenate([known, known[1:-1]])
        vertical = _normalise(_resample(angles, vals))
        fmt, complete = "radiomobile_v2", False
        warnings.append("Radio Mobile V2 files only hold the front half of the vertical cut; the back is mirrored.")
    else:
        vertical = np.zeros(360)
        fmt, complete = "radiomobile_v1", False
        warnings.append("Radio Mobile V1 files have no vertical pattern: elevation is treated as omnidirectional.")
    if n not in (360, 541, 540, 720):
        warnings.append(f"Unusual value count ({n}); read as {fmt.replace('_', ' ')}.")
    return Pattern(horizontal=horizontal, vertical=vertical, source_format=fmt, vertical_complete=complete,
                   warnings=warnings)


# --- detection -------------------------------------------------------------------
def parse_any(data: bytes, filename: str = "") -> Pattern:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    upper = text.upper()
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "adf" or re.search(r"^(REVNUM|PATCUT|NUPOIN|FSTLST):,", upper, re.M):
        pattern = parse_adf(text)
    elif re.search(r"^\s*HORIZONTAL\b", upper, re.M):
        pattern = parse_msi(text)
    elif ext in ("ant", "txt", "dat", "") and re.fullmatch(r"[\s0-9eE.+-]+", text):
        pattern = parse_radiomobile(text)
    else:
        raise PatternError("Unrecognised antenna file: expected MSI/Planet, NSMA .adf or Radio Mobile .ant")
    if not pattern.name and filename:
        pattern.name = filename.rsplit(".", 1)[0]
    return pattern


# --- generator ---------------------------------------------------------------------
def generate(kind: str, h_beamwidth: float = 65.0, v_beamwidth: float = 10.0,
             gain_dbi: float | None = None, front_to_back: float = 25.0,
             electrical_tilt: float = 0.0, side_lobe: float = 20.0, name: str = "") -> Pattern:
    """Parametric pattern (3GPP TR 36.814 style parabolic main lobe).

    Horizontal: A(phi) = min(12 (phi / HPBW)^2, FtB), or 0 for an omni.
    Vertical:   A(theta) = min(12 ((theta - tilt) / VPBW)^2, SLA) in front, the
    mirrored lobe plus the front-to-back ratio behind (omni: mirrored lobe only).
    """
    if kind not in ("omni", "sector"):
        raise PatternError("kind must be 'omni' or 'sector'")
    if not (1 <= v_beamwidth <= 180) or (kind == "sector" and not (1 <= h_beamwidth <= 360)):
        raise PatternError("beamwidths out of range")
    a = np.arange(360)
    phi = ((a + 180) % 360) - 180  # -180..179
    if kind == "omni":
        horizontal = np.zeros(360)
        back_extra = 0.0
    else:
        horizontal = np.minimum(12 * (phi / h_beamwidth) ** 2, front_to_back)
        back_extra = front_to_back
    d = ((a + 180) % 360) - 180  # downward angle -180..179
    front = np.abs(d) <= 90
    # Behind the mast the beam points down by the same tilt (seen mirrored).
    mirrored = np.where(d >= 0, 180 - d, -180 - d)
    theta = np.where(front, d, mirrored)
    lobe = np.minimum(12 * ((theta - electrical_tilt) / v_beamwidth) ** 2, side_lobe)
    vertical = np.where(front, lobe, np.minimum(lobe + back_extra, max(side_lobe, back_extra)))
    if gain_dbi is None:
        if kind == "sector":  # Kraus: D ~ 32400 / (HPBW x VPBW), allowing for typical efficiency
            gain_dbi = 10 * math.log10(32400.0 / (h_beamwidth * v_beamwidth))
        else:  # McDonald: D ~ 101 / (VPBW - 0.0027 VPBW^2) for omnidirectional antennas
            gain_dbi = 10 * math.log10(101.0 / (v_beamwidth - 0.0027 * v_beamwidth**2))
        gain_dbi = round(gain_dbi, 1)
    return Pattern(horizontal=_normalise(horizontal), vertical=_normalise(vertical), name=name,
                   gain_dbi=gain_dbi, electrical_tilt=electrical_tilt, source_format=f"generated_{kind}")


# --- Signal-Server output ----------------------------------------------------------
def ratio(att_db: np.ndarray) -> np.ndarray:
    return 10 ** (-np.asarray(att_db, dtype=float) / 20.0)


def to_az_el(p: Pattern) -> tuple[str, str]:
    """Signal-Server .az / .el text (first lines: rotation, then mechanical tilt + direction)."""
    az = ["0.0"] + [f"{a:d}\t{r:0.4f}" for a, r in enumerate(ratio(p.horizontal))]
    el = ["0.0\t0.0"] + [f"{d:d}\t{ratio(p.vertical[d % 360]):0.4f}" for d in range(-10, 91)]
    return "\n".join(az) + "\n", "\n".join(el) + "\n"


def from_az_el(az_text: str, el_text: str) -> Pattern:
    """Rebuild an (incomplete) pattern from Signal-Server files, for antennas saved earlier."""
    def rows(text):
        out = []
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                out.append((float(parts[0]), max(float(parts[1]), 1e-6)))
        return np.array(out)

    az, el = rows(az_text), rows(el_text)
    horizontal = _normalise(_resample(az[:, 0], -20 * np.log10(az[:, 1])))
    vertical = np.full(360, np.nan)
    for d, r in el:
        vertical[int(round(d)) % 360] = -20 * np.log10(r)
    known = ~np.isnan(vertical)
    vertical = _normalise(_resample(np.arange(360)[known], vertical[known]))
    return Pattern(horizontal=horizontal, vertical=vertical, source_format="signalserver",
                   vertical_complete=False)


def erp_watts(tx_power_w: float, gain_dbi: float, cable_loss_db: float = 0.0) -> float:
    """ERP (W, dipole-referenced) from transmitter power, antenna gain and feeder loss."""
    return tx_power_w * 10 ** ((gain_dbi - DBI_PER_DBD - cable_loss_db) / 10.0)
