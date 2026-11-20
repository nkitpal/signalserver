"""API + worker integration tests (PostGIS required, network not required)."""

import struct


def station(client, name, lat, lon, **kw):
    r = client.post("/api/stations", json={"name": name, "latitude": lat, "longitude": lon, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def plot_body(tx, antenna_id, **kw):
    body = {"name": "Alpine-UHF", "antenna_id": antenna_id, "tx_station_id": tx, "frequency_mhz": 446,
            "erp_watts": 25, "radius_km": 5, "rx_threshold": -110}
    body.update(kw)
    return body


def test_health_and_config(client):
    assert client.get("/api/health").json()["status"] == "ok"
    cfg = client.get("/api/config").json()
    assert cfg["engine_available"] is True
    assert "rainbow" in cfg["palettes"]
    assert cfg["propagation_models"]["1"].startswith("ITM")


def test_station_validation_and_geojson(client):
    assert client.post("/api/stations", json={"name": "Polar", "latitude": 80, "longitude": 0}).status_code == 422
    s = station(client, "Geo test", 47.3, 9.4, height_m=12)
    assert s["latitude"] == 47.3 and s["height_m"] == 12
    dup = client.post("/api/stations", json={"name": "Geo test", "latitude": 1, "longitude": 1})
    assert dup.status_code == 409
    feats = client.get("/api/stations.geojson").json()["features"]
    assert any(f["geometry"]["coordinates"][:2] == [9.4, 47.3] for f in feats)
    upd = client.put(f"/api/stations/{s['id']}", json={**s, "latitude": 47.31}).json()
    assert upd["latitude"] == 47.31
    assert client.delete(f"/api/stations/{s['id']}").status_code == 204


def test_full_run_with_elevation_download(client, worker, viewfinder, settings):
    antennas = client.get("/api/antennas").json()
    assert {a["name"] for a in antennas} >= {"Generic Dipole", "Generic Yagi"}
    ant = next(a for a in antennas if a["pattern"] == "dipole")
    tx = station(client, "Mountain", 47.25, 9.34, height_m=30)
    rx = station(client, "Valley", 47.42, 9.37, height_m=5)

    bad = client.post("/api/plots", json=plot_body(tx["id"], ant["id"], do_p2p_analysis=True))
    assert bad.status_code == 422
    r = client.post("/api/plots", json=plot_body(tx["id"], ant["id"], rx_station_id=rx["id"], do_p2p_analysis=True))
    assert r.status_code == 201, r.text
    plot = r.json()
    assert client.post("/api/plots", json=plot_body(tx["id"], ant["id"])).status_code == 409

    run = client.post(f"/api/plots/{plot['id']}/runs").json()
    assert run["status"] == "queued"
    assert worker.run_once() is True
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["status"] == "done", detail["error"]
    assert {"coverage.png", "coverage.tif", "coverage.kmz", "bundle.zip", "p2p_link.json"} <= set(detail["files"])
    assert detail["south"] < 47.25 < detail["north"] and detail["west"] < 9.34 < detail["east"]
    assert detail["report"]["signal_power_dbm"] == -76.0
    assert detail["legend"][0]["color"] == "#ff0000"
    assert any(url.endswith("/L32.zip") for url in viewfinder)

    png = client.get(detail["image_url"])
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    kmz = client.get(f"/api/runs/{run['id']}/files/coverage.kmz?download=true")
    assert kmz.content[:2] == b"PK" and "attachment" in kmz.headers["content-disposition"]
    assert client.get(f"/api/runs/{run['id']}/files/..%2Fsecret").status_code == 404

    # Elevation bookkeeping
    summary = client.get("/api/elevation/summary").json()
    assert summary["tiles_with_data"] >= 1 and summary["archives"] == 1
    tiles = client.get("/api/elevation/tiles.geojson").json()["features"]
    assert any(f["properties"]["lat"] == 47 and f["properties"]["lon"] == 9 for f in tiles)
    h = client.get("/api/elevation/height", params={"lat": 47.5, "lon": 9.5}).json()["height_m"]
    assert 400 <= h <= 900
    raw = client.get("/api/elevation/terrain/8/269/60.f32").content
    assert len(raw) == 32 * 32 * 4 and max(struct.unpack("<1024f", raw)) > 0

    # Spatial filter on run footprints
    near = client.get("/api/runs", params={"west": 9, "south": 47, "east": 10, "north": 48}).json()
    far = client.get("/api/runs", params={"west": 100, "south": 0, "east": 101, "north": 1}).json()
    assert run["id"] in [x["id"] for x in near] and not far

    # A second run reuses the cached archive.
    calls = len(viewfinder)
    client.post(f"/api/plots/{plot['id']}/runs")
    assert worker.run_once() is True
    assert len(viewfinder) == calls

    # Referential integrity and clean-up
    assert client.delete(f"/api/stations/{tx['id']}").status_code == 409
    assert client.delete(f"/api/plots/{plot['id']}").status_code == 204
    assert not (settings.runs_dir / str(run["id"])).exists()


def test_sea_tile_and_failure_reporting(client, worker, settings):
    ant = client.get("/api/antennas").json()[0]
    tx = station(client, "Buoy", 0.5, -30.5)  # mid-Atlantic: no archive covers it
    plot = client.post("/api/plots", json=plot_body(tx["id"], ant["id"], name="Ocean-plot")).json()
    run = client.post(f"/api/plots/{plot['id']}/runs").json()
    worker.run_once()
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["status"] == "done", detail["error"]
    tile = next(f for f in client.get("/api/elevation/tiles.geojson").json()["features"]
                if f["properties"]["lat"] == 0 and f["properties"]["lon"] == -31)
    assert tile["properties"]["has_data"] is False

    original = settings.signalserver_path
    settings.signalserver_path = "/nonexistent/signalserver"
    try:
        run = client.post(f"/api/plots/{plot['id']}/runs").json()
        worker.run_once()
    finally:
        settings.signalserver_path = original
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["status"] == "failed" and "not found" in detail["error"]


def test_prefetch_job(client, worker):
    too_big = client.post("/api/elevation/prefetch", json={"west": -50, "south": -10, "east": 50, "north": 10})
    assert too_big.status_code == 422
    job = client.post("/api/elevation/prefetch", json={"west": 9.2, "south": 46.2, "east": 9.8, "north": 46.8}).json()
    assert worker.run_once() is True
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "done"


def test_antenna_upload(client, settings):
    ant_text = "\n".join(["0"] * 360 + ["-3"] * 181) + "\n"
    r = client.post("/api/antennas", data={"name": "Test Panel", "category": "panel"},
                    files={"file": ("panel.ant", ant_text, "text/plain")})
    assert r.status_code == 201, r.text
    a = r.json()
    assert (settings.antenna_dir / "panel" / "Test_Panel.az").exists()
    bad = client.post("/api/antennas", data={"name": "Bad"}, files={"file": ("x.ant", "1 2 3", "text/plain")})
    assert bad.status_code == 422
    assert client.delete(f"/api/antennas/{a['id']}").status_code == 204
    assert not (settings.antenna_dir / "panel" / "Test_Panel.az").exists()
