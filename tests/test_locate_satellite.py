"""Transmitter localization and offline satellite packs."""

import math
import sqlite3

import numpy as np
import pytest


def test_logdistance_finds_transmitter():
    from types import SimpleNamespace

    from signalserver_gui.localize import _rms, _score_logdistance

    tx_lat, tx_lon = 47.30, 9.40
    pts = [(47.35, 9.35), (47.25, 9.45), (47.33, 9.48), (47.27, 9.33), (47.36, 9.42), (47.24, 9.39)]
    ms = []
    for la, lo in pts:
        d = math.hypot((la - tx_lat) * 111.2, (lo - tx_lon) * 111.2 * math.cos(math.radians(47.3)))
        ms.append({"latitude": la, "longitude": lo, "dbm": -40 - 30 * math.log10(d)})  # n = 3
    loc = SimpleNamespace(radius_km=5.0)
    box, lats, lons, est, n = _score_logdistance(loc, ms)
    rms, eirp = _rms(est, len(ms))
    r, c = np.unravel_index(np.nanargmin(rms), rms.shape)
    err = math.hypot((lats[r, c] - tx_lat) * 111.2, (lons[r, c] - tx_lon) * 111.2 * 0.68)
    assert err < 0.3
    # With a free exponent, a few tens of metres of position shift trade off against the slope at
    # almost no residual cost, so the fitted exponent is only loosely constrained.
    assert n[r, c] == pytest.approx(3.0, abs=0.4) and eirp[r, c] == pytest.approx(-40, abs=2)


def test_localization_api_terrain(client, worker):
    p = client.post("/api/projects", json={"name": "Locate"}).json()
    body = {"name": "Unknown 446", "method": "terrain", "frequency_mhz": 446, "radius_km": 2,
            "measurements": [{"latitude": 47.30, "longitude": 9.40, "dbm": -70},
                             {"latitude": 47.32, "longitude": 9.42, "dbm": -80},
                             {"latitude": 47.29, "longitude": 9.43, "dbm": -85}]}
    assert client.post(f"/api/projects/{p['id']}/localizations",
                       json={**body, "measurements": body["measurements"][:2]}).status_code == 422
    r = client.post(f"/api/projects/{p['id']}/localizations", json=body)
    assert r.status_code == 202, r.text
    loc = r.json()
    assert loc["status"] == "queued"
    assert worker.run_once()
    d = client.get(f"/api/localizations/{loc['id']}").json()
    assert d["status"] == "done", d["error"]
    res = d["result"]
    assert 47.2 < res["latitude"] < 47.4 and 9.3 < res["longitude"] < 9.5
    assert res["candidates_tested"] > 169 and len(res["residuals_db"]) == 3 and "eirp_dbm" in res
    png = client.get(d["image_url"])
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    # Re-run with the distance-only method.
    r = client.put(f"/api/localizations/{loc['id']}", json={**body, "method": "logdistance"})
    assert r.status_code == 202 and r.json()["result"] is None
    assert worker.run_once()
    d = client.get(f"/api/localizations/{loc['id']}").json()
    assert d["status"] == "done" and "path_loss_exponent" in d["result"]
    assert client.delete(f"/api/localizations/{loc['id']}").status_code == 204


def test_satellite_pack_and_mbtiles_import(client, worker, tmp_path):
    est = client.get("/api/basemaps/estimate", params={"west": 9.3, "south": 47.38, "east": 9.35,
                                                       "north": 47.42, "max_zoom": 15, "kind": "satellite"}).json()
    r = client.post("/api/basemaps", json={"name": "Sat test", "west": 9.3, "south": 47.38, "east": 9.35,
                                           "north": 47.42, "max_zoom": 8, "kind": "satellite",
                                           "with_elevation": False})
    assert r.status_code == 202, r.text
    assert worker.run_once()
    pack = next(x for x in client.get("/api/basemaps").json() if x["name"] == "Sat test")
    assert pack["ready"] and pack["kind"] == "raster" and pack["tiles_url"] and est["ok"]
    from signalserver_gui.basemap import lonlat_to_tile

    x, y = lonlat_to_tile(9.32, 47.40, 8)
    tile = client.get(pack["tiles_url"].format(z=8, x=x, y=y))
    assert tile.status_code == 200 and tile.headers["content-type"] == "image/jpeg"
    empty = client.get(pack["tiles_url"].format(z=8, x=0, y=0))
    assert empty.headers["content-type"] == "image/png"  # transparent outside the pack

    db = tmp_path / "mine.mbtiles"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE metadata (name text, value text)")
    con.execute("CREATE TABLE tiles (zoom_level int, tile_column int, tile_row int, tile_data blob)")
    con.executemany("INSERT INTO metadata VALUES (?, ?)", [("name", "drone"), ("format", "png"), ("bounds", "9.3,47.38,9.35,47.42")])
    con.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", (10, 538, (2**10 - 1) - 361, b"\x89PNGdata"))
    con.commit()
    con.close()
    r = client.post("/api/basemaps/import", data={"name": "Drone"}, files={"file": ("mine.mbtiles", db.read_bytes())})
    assert r.status_code == 201, r.text
    imp = r.json()
    assert imp["kind"] == "raster" and imp["tile_count"] == 1 and imp["west"] == 9.3
    t = client.get(imp["tiles_url"].format(z=10, x=538, y=361))
    assert t.content == b"\x89PNGdata" and t.headers["content-type"] == "image/png"
    assert client.post("/api/basemaps/import", data={"name": "Bad"}, files={"file": ("x.mbtiles", b"nope")}).status_code == 422
