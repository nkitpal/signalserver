"""Antenna importers, generator, Signal-Server output and the antenna/ERP API."""

import numpy as np
import pytest

from signalserver_gui.engine.antennas import (
    PatternError,
    erp_watts,
    from_az_el,
    generate,
    parse_any,
    parse_adf,
    parse_msi,
    parse_radiomobile,
    to_az_el,
)


def msi_text(gain="14.5 dBi", tilt="ELECTRICAL 4"):
    h = "\n".join(f"{a} {min(12 * (((a + 180) % 360 - 180) / 65) ** 2, 25):.2f}" for a in range(360))
    # Vertical: main lobe 4 degrees below the horizon (downward angle 4).
    v = "\n".join(f"{a} {min(12 * ((((a + 180) % 360 - 180) - 4) / 10) ** 2, 20):.2f}" for a in range(360))
    return f"""Sector-65-14
MAKE Acme Antennas
FREQUENCY 1800
H_WIDTH 65
V_WIDTH 10
GAIN {gain}
TILT {tilt}
POLARIZATION POL_V
COMMENT test pattern
HORIZONTAL 360
{h}
VERTICAL 360
{v}
"""


ADF = """REVNUM:,TIA/EIA-804-B
COMNT1:,Standard TIA/EIA Antenna Pattern Data
ANTMAN:,RFI Antennas
MODNUM:,CC806-06 @ 870
DESCR1:,Corporate collinear
LOWFRQ:,746
HGHFRQ:,870
GUNITS:,DBD/DBR
MDGAIN:,4.8
ELTILT:,0
PATTYP:,Typical
NOFREQ:,1
PATFRE:,870
NUMCUT:,2
PATCUT:,V
POLARI:,V/V
NUPOIN:,8
FSTLST:,-135,180
-135,-20.0
-90,-40.0
-45,-20.0
-10,-6.0
0,0.000
10,-1.0
90,-35.0
180,-0.5
PATCUT:,H
POLARI:,V/V
NUPOIN:,4
FSTLST:,-90,180
-90,-1.0
0,0.0
90,-1.0
180,-1.1
ENDFIL:,EOF
"""


# --- importers -----------------------------------------------------------------
def test_msi_import():
    p = parse_any(msi_text().encode(), "sector.msi")
    assert p.source_format == "msi" and p.name == "Sector-65-14" and p.manufacturer == "Acme Antennas"
    assert p.gain_dbi == 14.5 and p.frequency_mhz == 1800 and p.electrical_tilt == 4
    m = p.metrics()
    assert m["h_beamwidth"] == pytest.approx(65, abs=2) and m["front_to_back"] == 25.0
    assert m["peak_tilt"] == 4 and m["v_beamwidth"] == pytest.approx(10, abs=2)
    # dBd is the default gain unit; .pln is the same format
    assert parse_any(msi_text(gain="12").encode(), "x.pln").gain_dbi == 14.15
    assert parse_msi("NAME Only horizontal\nHORIZONTAL 4\n0 0\n90 3\n180 20\n270 3\n").vertical_complete is False


def test_msi_errors():
    with pytest.raises(PatternError):
        parse_msi("NAME x\nHORIZONTAL 360\n0 0\n1 1\n")
    with pytest.raises(PatternError):
        parse_any(b"hello world", "x.msi")


def test_adf_import_and_conventions():
    p = parse_any(ADF.encode(), "cc806.adf")
    assert p.source_format == "adf" and p.manufacturer == "RFI Antennas" and p.name == "CC806-06 @ 870"
    assert p.gain_dbi == pytest.approx(6.95) and p.frequency_mhz == 870 and p.electrical_tilt == 0
    # Horizontal: interpolated, relative dB -> attenuation
    assert p.horizontal[0] == 0 and p.horizontal[90] == pytest.approx(1.0) and p.horizontal[270] == pytest.approx(1.0)
    # V cut angles are elevation (up positive): +90 (zenith) -> downward angle 270, -90 (nadir) -> 90
    assert p.vertical[270] == pytest.approx(35.0) and p.vertical[90] == pytest.approx(40.0)
    assert p.vertical[10] == pytest.approx(6.0)  # 10 deg below the horizon = elevation -10
    flipped = p.flipped()
    assert flipped.vertical[90] == pytest.approx(35.0) and flipped.vertical[350] == pytest.approx(6.0)
    with pytest.raises(PatternError):
        parse_adf("REVNUM:,TIA/EIA-804-B\nENDFIL:,EOF\n")


def test_radiomobile_versions():
    h = [-min(12 * (((a + 180) % 360 - 180) / 40) ** 2, 20) for a in range(360)]
    v1 = parse_radiomobile("\n".join(f"{x:.2f}" for x in h))
    assert v1.source_format == "radiomobile_v1" and not v1.vertical.any()
    v2_vert = [-(abs(90 - k - (-5)) / 10) for k in range(181)]  # minimum at elevation -5 (5 deg down)
    v2 = parse_radiomobile("\n".join(f"{x:.3f}" for x in h + v2_vert))
    assert v2.source_format == "radiomobile_v2" and v2.metrics()["peak_tilt"] == 5
    v3_vert = [-(abs(((90 - k + 180) % 360 - 180) - (-3)) / 10) for k in range(360)]  # peak at elevation -3
    v3 = parse_any("\n".join(f"{x:.3f}" for x in h + v3_vert).encode(), "y.ant")
    assert v3.source_format == "radiomobile_v3" and v3.metrics()["peak_tilt"] == 3
    with pytest.raises(PatternError):
        parse_radiomobile("0 -1 -2")


def test_az_el_output_and_roundtrip():
    p = parse_any(msi_text().encode(), "sector.msi")
    az, el = to_az_el(p)
    az_lines, el_lines = az.splitlines(), el.splitlines()
    assert az_lines[0] == "0.0" and len(az_lines) == 361 and az_lines[1] == "0\t1.0000"
    assert el_lines[0] == "0.0\t0.0" and len(el_lines) == 102 and el_lines[1].startswith("-10\t")
    assert el_lines[15] == "4\t1.0000"  # main lobe at 4 degrees downtilt
    back = from_az_el(az, el)
    assert np.allclose(back.horizontal, p.horizontal, atol=0.01)
    assert np.allclose(back.vertical[:91], p.vertical[:91], atol=0.01)


def test_generator():
    s = generate("sector", 65, 10, None, 25, 6)
    m = s.metrics()
    assert m["h_beamwidth"] == pytest.approx(65, abs=2) and m["v_beamwidth"] == pytest.approx(10, abs=2)
    assert m["peak_tilt"] == 6 and m["front_to_back"] == 25.0
    assert s.gain_dbi == pytest.approx(17.0, abs=0.2)
    o = generate("omni", 360, 15, None, 0, 0)
    assert o.metrics()["h_beamwidth"] == 360 and o.gain_dbi == pytest.approx(8.5, abs=0.1)  # McDonald: 101 / (15 - 0.0027 * 15**2)
    assert generate("sector", 90, 8, 15.5).gain_dbi == 15.5
    with pytest.raises(PatternError):
        generate("dish")


def test_erp():
    assert erp_watts(10, 2.15) == pytest.approx(10)
    assert erp_watts(10, 17.15, 2.0) == pytest.approx(10 * 10 ** 1.3)


# --- API ---------------------------------------------------------------------
def test_antenna_api(client, settings):
    prev = client.post("/api/antennas/preview", files={"file": ("cc806.adf", ADF.encode())})
    assert prev.status_code == 200, prev.text
    data = prev.json()
    assert data["source_format"] == "adf" and len(data["horizontal"]) == 360 and data["gain_dbi"] == pytest.approx(6.95)
    flipped = client.post("/api/antennas/preview", files={"file": ("cc806.adf", ADF.encode())},
                          data={"flip_vertical": "true"}).json()
    assert flipped["vertical"][90] == data["vertical"][270]
    assert client.post("/api/antennas/preview", files={"file": ("x.doc", b"x")}).status_code == 422
    assert client.post("/api/antennas/preview", files={"file": ("x.msi", b"nonsense")}).status_code == 422

    r = client.post("/api/antennas", files={"file": ("sector.msi", msi_text().encode())},
                    data={"category": "panel"})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["name"] == "Sector-65-14" and a["gain_dbi"] == 14.5 and a["manufacturer"] == "Acme Antennas"
    assert a["source_format"] == "msi" and a["h_beamwidth"] == pytest.approx(65, abs=2) and a["electrical_tilt"] == 4
    base = settings.antenna_dir / "panel" / "Sector-65-14"
    assert base.with_suffix(".msi").exists() and base.with_suffix(".az").exists() and base.with_suffix(".el").exists()
    pat = client.get(f"/api/antennas/{a['id']}/pattern").json()
    assert len(pat["vertical"]) == 360 and pat["vertical_complete"]
    assert client.post("/api/antennas", files={"file": ("sector.msi", msi_text().encode())}).status_code == 409

    g = client.post("/api/antennas/generate", params={"preview": "true"},
                    json={"kind": "sector", "h_beamwidth": 90, "v_beamwidth": 8, "electrical_tilt": 3})
    assert g.status_code == 200 and g.json()["peak_tilt"] == 3 and "id" not in g.json()
    g = client.post("/api/antennas/generate", json={"kind": "omni", "v_beamwidth": 15})
    assert g.status_code == 200, g.text
    omni = g.json()
    assert omni["name"].startswith("Omni") and omni["source_format"] == "generated_omni"

    # Bundled antennas have metadata after seeding; legacy rows rebuild from .az/.el.
    bundled = {x["name"]: x for x in client.get("/api/antennas").json()}
    assert bundled["Generic Dipole"]["gain_dbi"] == 2.15
    assert bundled["Generic Yagi"]["source_format"] == "radiomobile_v2"

    upd = client.patch(f"/api/antennas/{bundled['Generic Yagi']['id']}", json={"gain_dbi": 9.5})
    assert upd.json()["gain_dbi"] == 9.5
    ok = client.delete(f"/api/antennas/{a['id']}")
    assert ok.status_code == 204 and not base.with_suffix(".msi").exists()


def test_plot_erp_from_tx_power(client, worker):
    p = client.post("/api/projects", json={"name": "ERP"}).json()
    st = client.post("/api/stations", json={"project_id": p["id"], "name": "Mast", "latitude": 47.2,
                                            "longitude": 9.5}).json()
    ants = {x["name"]: x for x in client.get("/api/antennas").json()}
    no_gain = client.post("/api/antennas/generate", json={"kind": "sector", "name": "No gain yet",
                                                          "h_beamwidth": 65, "v_beamwidth": 10}).json()
    client.patch(f"/api/antennas/{no_gain['id']}", json={"gain_dbi": None})
    base = {"project_id": p["id"], "tx_station_id": st["id"], "frequency_mhz": 446, "radius_km": 3}
    bad = client.post("/api/plots", json={**base, "name": "ERP-bad", "antenna_id": no_gain["id"], "tx_power_w": 10})
    assert bad.status_code == 422 and "gain" in bad.text
    plot = client.post("/api/plots", json={**base, "name": "ERP-ok", "antenna_id": ants["Generic Dipole"]["id"],
                                           "tx_power_w": 25, "cable_loss_db": 3.0, "erp_watts": 999})
    assert plot.status_code == 201, plot.text
    body = plot.json()
    assert body["erp_watts"] == pytest.approx(25 * 10 ** (-0.3), rel=1e-4) and body["tx_power_w"] == 25
    run = client.post(f"/api/plots/{body['id']}/runs").json()
    assert worker.run_once()
    d = client.get(f"/api/runs/{run['id']}").json()
    assert d["params"]["erp_watts"] == pytest.approx(25 * 10 ** (-0.3), rel=1e-4)
    assert d["params"]["antenna"]["gain_dbi"] == 2.15
    i = d["command"].index("-erp")
    assert float(d["command"][i + 1]) == pytest.approx(12.53, abs=0.01)
