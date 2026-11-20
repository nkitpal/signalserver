"""Unit tests that need no database or network."""

from pathlib import Path

import numpy as np
import pytest

from signalserver_gui.elevation.index import archives_for_tile, load_index, parse_coverage_page
from signalserver_gui.elevation.tiles import (
    TileId,
    coverage_bbox,
    read_hgt,
    sdf_body,
    tiles_for_bbox,
    write_sdf,
)
from signalserver_gui.engine.command import build_args
from signalserver_gui.engine.outputs import parse_bounds
from signalserver_gui.engine.patterns import ant_to_az_el, read_palette
from signalserver_gui.engine.report import parse_report

RES = Path(__file__).parent.parent / "signalserver_gui" / "resources"
FIXTURES = Path(__file__).parent / "fixtures"


# Tiles ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "lat,lon,hgt,sdf",
    [
        (51, -1, "N51W001.hgt", ["51_52_0_1.sdf", "51_52_360_361.sdf"]),
        (51, 0, "N51E000.hgt", ["51_52_359_360.sdf", "51_52_-1_0.sdf"]),
        (46, 9, "N46E009.hgt", ["46_47_350_351.sdf"]),
        (-34, 151, "S34E151.hgt", ["-34_-33_208_209.sdf"]),
        (40, -180, "N40W180.hgt", ["40_41_179_180.sdf"]),
        (40, 179, "N40E179.hgt", ["40_41_180_181.sdf"]),
    ],
)
def test_tile_names(lat, lon, hgt, sdf):
    t = TileId(lat, lon)
    assert t.hgt_name == hgt
    assert t.sdf_names() == sdf
    assert TileId.from_hgt_name(hgt) == t


def test_tiles_for_bbox_simple_and_antimeridian():
    assert tiles_for_bbox(-0.5, 51.2, 0.5, 51.8) == [TileId(51, -1), TileId(51, 0)]
    wrap = tiles_for_bbox(179.5, 10.2, -179.5, 10.4)
    assert wrap == [TileId(10, -180), TileId(10, 179)]


def test_coverage_bbox_contains_engine_box():
    # Signal-Server reported 51.410119..51.589881 / -0.644..-0.356 for R=10 km.
    w, s, e, n = coverage_bbox(51.5, -0.5, 10)
    assert w < -0.644 and e > -0.356 and s < 51.4101 and n > 51.5898


def test_sdf_body_matches_srtm2sdf_order():
    grid = np.arange(1201 * 1201, dtype=np.int32).reshape(1201, 1201)
    body = sdf_body(grid)
    # srtm2sdf: for y in ippd..1, for x in mpi..0: srtm[y][x]
    expected = [grid[y][x] for y in range(1200, 1197, -1) for x in range(1199, -1, -1)]
    assert body.ravel()[: len(expected)].tolist() == expected
    assert body.shape == (1200, 1200)


def test_write_sdf_headers(tmp_path):
    grid = np.full((1201, 1201), 7, dtype=np.int16)
    paths = write_sdf(TileId(51, 0), grid, tmp_path)
    assert {p.name for p in paths} == {"51_52_359_360.sdf", "51_52_-1_0.sdf"}
    lines = (tmp_path / "51_52_359_360.sdf").read_text().splitlines()
    assert lines[:4] == ["360", "51", "359", "52"]
    assert len(lines) == 4 + 1200 * 1200 and lines[4] == "7"
    sea = write_sdf(TileId(0, -30), None, tmp_path)[0].read_text().splitlines()
    assert sea[4:6] == ["0", "0"]


def test_read_hgt(tmp_path):
    arr = np.arange(1201 * 1201, dtype=">i2").reshape(1201, 1201)
    p = tmp_path / "N10E010.hgt"
    arr.tofile(p)
    assert read_hgt(p)[0, 5] == 5


# Viewfinder index -----------------------------------------------------------
def test_parse_coverage_page():
    html = (
        '<area shape="rect" coords="870, 190, 900, 210" href="http://viewfinderpanoramas.org/dem3/M30.zip">'
        '<area shape="rect" coords="0, 750, 450, 900" href="https://viewfinderpanoramas.org/ANTDEM3/01-15.zip">'
    )
    a, b = parse_coverage_page(html)
    assert (a.name, a.west, a.south, a.east, a.north) == ("M30", -6.0, 48.0, 0.0, 52.0)
    assert a.url.startswith("https://")
    assert (b.west, b.south, b.east, b.north) == (-180.0, -90.0, -90.0, -60.0)


def test_bundled_index_lookups():
    index = load_index()
    assert len(index) > 1000
    assert [a.name for a in archives_for_tile(51, -1, index)] == ["M30"]
    assert [a.name for a in archives_for_tile(46, 7, index)] == ["L32"]
    assert archives_for_tile(64, -20, index)[0].name == "ISL"


# Engine helpers -------------------------------------------------------------
def test_parse_bounds():
    out = "...\n[info] Area boundaries:51.589881 | -0.355000 | 51.410119 | -0.645000 \n"
    assert parse_bounds(out) == (51.589881, -0.355, 51.410119, -0.645)
    assert parse_bounds("Area boundaries:103.1 | 292.5 | -0.1 | -293.5") is None
    assert parse_bounds("nothing") is None


def test_ant_conversion_matches_bundled_files():
    az, el = ant_to_az_el((RES / "antennas/dipole/dipole.ant").read_text())
    ref_az = (RES / "antennas/dipole/dipole.az").read_text().splitlines()
    ref_el = (RES / "antennas/dipole/dipole.el").read_text().splitlines()
    assert az.splitlines()[1:] == ref_az[1:]
    assert el.splitlines()[1:] == ref_el[1:]


def test_read_palette():
    entries = read_palette(RES / "color_profiles/rainbow.dcf")
    assert entries[0] == {"value": 0.0, "color": "#ff0000"}
    assert len(entries) >= 10


def test_parse_report():
    r = parse_report((FIXTURES / "p2p_report.txt").read_text())
    assert r["tx"]["latitude"] == 51.5 and r["rx"]["longitude"] == -0.45
    assert r["distance"] == 6.55 and r["distance_units"] == "kilometers"
    assert r["signal_power_dbm"] == -76.0
    assert r["computed_path_loss_db"] == 122.12
    assert len(r["obstructions"]) == 4 and r["obstructions"][0]["longitude"] == -0.4558
    assert r["line_of_sight_clear"] is False
    assert r["rx_height_to_clear_fresnel60"] == 41.01


def _params(**over):
    p = {
        "name": "t", "frequency_mhz": 446.0, "erp_watts": 10.0, "radius_km": 10.0, "resolution": 1200,
        "rx_height_m": 1.5, "rx_threshold": -100.0, "propagation_model": 1, "propagation_mode": None,
        "terrain_code": None, "terrain_dielectric": None, "terrain_conductivity": None, "climate_code": 5,
        "reliability": None, "confidence": None, "ground_clutter_m": None, "knife_edge": True,
        "use_dbm": True, "metric_units": True, "do_p2p_analysis": False, "color_palette": "rainbow",
        "opacity": 0.7,
        "antenna": {"name": "a", "category": "dipole", "pattern": "dipole", "rx_gain_dbd": 2.0},
        "tx": {"name": "tx", "latitude": 47.1, "longitude": 9.05, "height_m": 30.0, "polarization": "horizontal",
               "rotation_deg": 90.0, "downtilt_deg": 0.0, "downtilt_direction_deg": 0.0},
        "rx": {"name": "rx", "latitude": 47.2, "longitude": 9.1, "height_m": 3.0, "polarization": "vertical",
               "rotation_deg": 0.0, "downtilt_deg": 0.0, "downtilt_direction_deg": 0.0},
    }
    p.update(over)
    return p


def _flag(args, flag):
    return args[args.index(flag) + 1]


def test_build_args_metric(tmp_path):
    args = build_args(_params(), sdf_dir=tmp_path, antenna_dir=RES / "antennas", palette=None,
                      output_base=Path("coverage"))
    assert _flag(args, "-txh") == "30" and _flag(args, "-R") == "10"
    assert "-m" in args and "-ked" in args and "-dbm" in args and "-hp" in args
    assert _flag(args, "-rot") == "90" and _flag(args, "-cl") == "5"
    assert _flag(args, "-ant").endswith("antennas/dipole/dipole")
    assert args[-2:] == ["-o", "coverage"] and "-rla" not in args


def test_build_args_imperial_and_p2p(tmp_path):
    args = build_args(_params(metric_units=False), sdf_dir=tmp_path, antenna_dir=RES / "antennas",
                      palette=None, output_base=Path("p2p_link"), p2p=True)
    assert "-m" not in args
    assert float(_flag(args, "-txh")) == pytest.approx(98.4252, rel=1e-4)
    assert float(_flag(args, "-R")) == pytest.approx(6.21371, rel=1e-4)
    assert _flag(args, "-rla") == "47.2" and float(_flag(args, "-rxh")) == pytest.approx(9.84252, rel=1e-4)
    assert _flag(args, "-rxg") == "2" and "-ng" in args
