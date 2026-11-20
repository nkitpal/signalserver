"""Unit tests for value rasters, Copernicus/buildings, KML parsing and map packs."""

import io
import zipfile

import numpy as np
import pytest

from signalserver_gui.basemap import count_tiles, lonlat_to_tile, tile_ids_for
from signalserver_gui.elevation.buildings import burn_buildings, parse_height, parse_overpass
from signalserver_gui.elevation.copernicus import resample_to_grid, tile_name
from signalserver_gui.elevation.tiles import TileId, downsample_to_dem3, sdf_body, write_sdf
from signalserver_gui.engine.values import (
    NODATA_INDEX,
    QUANTITIES,
    colorize,
    decode_indices,
    index_to_values,
    quantity_for,
    write_measurement_palette,
)
from signalserver_gui.layers import LayerError, kml_bytes, parse_kml


# Value rasters ---------------------------------------------------------------
@pytest.mark.parametrize("kind", ["dbm", "dbuvm", "loss"])
def test_measurement_palette_roundtrip(tmp_path, kind):
    q = QUANTITIES[kind]
    name = write_measurement_palette(q, tmp_path)
    assert name.endswith(q.ext) and "/" not in name
    lines = (tmp_path / name).read_text().splitlines()
    assert len(lines) == 128
    colors = np.array([[int(c) for c in ln.split(":")[1].split(",")] for ln in lines], dtype=np.uint8)
    assert len({tuple(c) for c in colors}) == 128  # unique colours
    img = np.full((2, 130, 3), 255, dtype=np.uint8)
    img[0, :128] = colors
    idx = decode_indices(img)
    assert idx[0, :128].tolist() == list(range(128))
    assert (idx[1] == NODATA_INDEX).all() and idx[0, 128] == NODATA_INDEX
    vals = index_to_values(idx, q)
    assert vals[0, 0] == q.top and vals[0, 5] == q.top + 5 * q.step and vals[1, 0] == -32768


def test_measurement_levels_follow_engine_band_rules(tmp_path):
    q = QUANTITIES["dbm"]
    levels = [int(ln.split(":")[0]) for ln in (tmp_path / write_measurement_palette(q, tmp_path)).read_text().splitlines()]
    assert levels[:3] == [-30, -31, -32] and levels[-1] == -157
    q = QUANTITIES["loss"]
    levels = [int(ln.split(":")[0]) for ln in (tmp_path / write_measurement_palette(q, tmp_path)).read_text().splitlines()]
    assert levels[:2] == [70, 72]  # band 0 = <=70 dB, band i = [68+2i, 70+2i)


def test_quantity_for():
    assert quantity_for({"erp_watts": 10, "use_dbm": True}).kind == "dbm"
    assert quantity_for({"erp_watts": 10, "use_dbm": False}).kind == "dbuvm"
    assert quantity_for({"erp_watts": 0, "use_dbm": True}).kind == "loss"


def test_colorize_matches_engine_band_rules():
    palette = [{"value": 0, "color": "#ff0000"}, {"value": -50, "color": "#00ff00"}, {"value": -100, "color": "#0000ff"}]
    q = QUANTITIES["dbm"]
    # values: -30 (>=0? no -> >=-50 green), -60 (blue band >= -100), -120 (below last level: none)
    idx = np.array([[0, 30, 90, NODATA_INDEX]], dtype=np.uint8)
    rgba = colorize(idx, q, palette)
    assert rgba[0, 0].tolist() == [0, 255, 0, 255]
    assert rgba[0, 1].tolist() == [0, 0, 255, 255]
    assert rgba[0, 2, 3] == 0 and rgba[0, 3, 3] == 0
    loss = QUANTITIES["loss"]
    lp = [{"value": 80, "color": "#ff0000"}, {"value": 100, "color": "#00ff00"}]
    idx = np.array([[0, 10, 20]], dtype=np.uint8)  # 68 (<=80 band 0), 88 (band 1), 108 (>= last: none)
    rgba = colorize(idx, loss, lp)
    assert rgba[0, 0, :3].tolist() == [255, 0, 0] and rgba[0, 1, :3].tolist() == [0, 255, 0]
    assert rgba[0, 2, 3] == 0


# Copernicus / HD SDF -----------------------------------------------------------
def test_copernicus_resample_and_names():
    assert tile_name(TileId(47, 9)) == "Copernicus_DSM_COG_10_N47_00_E009_00_DEM"
    assert tile_name(TileId(-34, -71)) == "Copernicus_DSM_COG_10_S34_00_W071_00_DEM"
    data = np.arange(3600, dtype=np.float32)[None, :].repeat(3600, axis=0)
    grid = resample_to_grid(data)
    assert grid.shape == (3601, 3601) and grid[0, 0] == 0 and grid[0, 3599] == 3599 and grid[3600, 10] == 10
    narrow = np.tile(np.arange(2400, dtype=np.float32) * 1.5, (3600, 1))  # 2400 columns above 50 deg
    g2 = resample_to_grid(narrow)
    assert g2[0, 1800] == pytest.approx(1800, abs=1)  # lon offset 0.5 deg maps to the same place
    nodata = resample_to_grid(np.full((3600, 3600), -9999, np.float32), nodata=-9999)
    assert (nodata == 0).all()


def test_hd_sdf(tmp_path):
    t = TileId(47, 9)
    assert t.sdf_names(hd=True) == ["47_48_350_351-hd.sdf"]
    assert TileId(51, 0).sdf_names(hd=True) == ["51_52_359_360-hd.sdf", "51_52_-1_0-hd.sdf"]
    grid = np.arange(3601 * 3601, dtype=np.int32).reshape(3601, 3601) % 1000
    body = sdf_body(grid.astype(np.int16), 3600)
    assert body.shape == (3600, 3600) and body[0, 0] == grid[3600, 3599]
    with pytest.raises(ValueError):
        sdf_body(grid[:1201, :1201].astype(np.int16), 3600)
    assert downsample_to_dem3(grid).shape == (1201, 1201)
    path = write_sdf(t, None, tmp_path, hd=True)[0]
    lines = path.read_text().splitlines()
    assert lines[:4] == ["351", "47", "350", "48"] and len(lines) == 4 + 3600 * 3600


# Buildings -------------------------------------------------------------------
@pytest.mark.parametrize("tags,expected", [
    ({"height": "12"}, 12.0), ({"height": "12.5 m"}, 12.5), ({"height": "40 ft"}, 12.192),
    ({"building:levels": "4"}, 12.0), ({"building:levels": "3", "roof:levels": "1"}, 10.0),
    ({"height": "tall"}, 6.0), ({}, 6.0), ({"height": "5000"}, 6.0),
])
def test_parse_height(tags, expected):
    assert parse_height(tags, 6.0) == pytest.approx(expected)


def test_burn_buildings():
    from tests.conftest import OVERPASS_RESPONSE

    tile = TileId(47, 9)
    items = parse_overpass(OVERPASS_RESPONSE, 6.0)
    assert len(items) == 2 and items[0][1] == 30.0 and items[1][1] == 6.0
    grid = np.full((3601, 3601), 400, dtype=np.int16)
    raised, n = burn_buildings(grid, tile, items)
    assert n == 2
    r = round((48 - 47.3005) * 3600)
    c = round((9.3005 - 9) * 3600)
    assert raised[r, c] == 430  # ground 400 + 30 m
    # The small house (smaller than a cell) still raises the cell at its centroid.
    rs, cs = round((48 - 47.31003) * 3600), round((9.31007 - 9) * 3600)
    assert raised[rs, cs] == 406
    assert raised[100, 100] == 400  # untouched elsewhere
    # A surface model already higher than ground + building (e.g. a 3x3 cell canopy;
    # the 5x5 ground window still sees 400) is left alone.
    tall = grid.copy()
    tall[r - 1 : r + 2, c - 1 : c + 2] = 500
    assert burn_buildings(tall, tile, items)[0][r, c] == 500


# KML -------------------------------------------------------------------------
KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Sites</name>
<Folder><Placemark><name>A</name><description>first</description><Point><coordinates>9.4,47.3,12</coordinates></Point></Placemark>
<Placemark><name>Line</name><LineString><coordinates>9.0,47.0 10.0,48.0</coordinates></LineString></Placemark>
<Placemark><Point><coordinates> 8.5 , 46.5 </coordinates></Point></Placemark></Folder></Document></kml>"""


def test_parse_kml_and_kmz():
    parsed = parse_kml(KML)
    assert parsed.name == "Sites" and parsed.feature_count == 3
    assert parsed.points[0] == {"name": "A", "latitude": 47.3, "longitude": 9.4, "altitude": 12.0, "description": "first"}
    assert parsed.bounds == (9.0, 47.0, 10.0, 48.0)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("doc.kml", KML)
        zf.writestr("files/icon.png", b"x")
    assert kml_bytes(buf.getvalue(), "x.kmz") == KML
    with pytest.raises(LayerError):
        parse_kml(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><kml>&a;</kml>')
    with pytest.raises(LayerError):
        parse_kml(b"<kml><unclosed></kml>")
    with pytest.raises(LayerError):
        kml_bytes(b"PK\x03\x04garbage", "bad.kmz")


# Basemap tiles ---------------------------------------------------------------
def test_basemap_tile_math():
    assert lonlat_to_tile(0, 0, 1) == (1, 1)
    assert lonlat_to_tile(-180, 85.06, 3) == (0, 0)
    ids = tile_ids_for(9.30, 47.38, 9.45, 47.47, 8)
    assert len(ids) == count_tiles(9.30, 47.38, 9.45, 47.47, 8) == len(set(ids))
    assert ids == sorted(ids)
    assert count_tiles(9.30, 47.38, 9.45, 47.47, 4) == 1 + 4 + 16 + 64 + 256
