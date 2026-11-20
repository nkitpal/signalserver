"""Integration tests: projects/folders, KML layers, value rasters, elevation models, map packs."""

import io
import zipfile

KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Survey</name>
<Placemark><name>P1</name><Point><coordinates>9.30,47.25</coordinates></Point></Placemark>
<Placemark><name>P2</name><Point><coordinates>9.35,47.27</coordinates></Point></Placemark>
<Placemark><name>Far north</name><Point><coordinates>20.0,75.0</coordinates></Point></Placemark>
<Placemark><name>Route</name><LineString><coordinates>9.3,47.2 9.4,47.3</coordinates></LineString></Placemark>
</Document></kml>"""


def ok(resp, code=200):
    assert resp.status_code == code, resp.text
    return resp.json() if resp.content and resp.headers.get("content-type", "").startswith("application/json") else resp


def test_projects_folders_and_moves(client):
    p = ok(client.post("/api/projects", json={"name": "Alps network"}), 201)
    assert client.post("/api/projects", json={"name": "Alps network"}).status_code == 409
    sites = ok(client.post(f"/api/projects/{p['id']}/folders", json={"name": "Sites"}), 201)
    hill = ok(client.post(f"/api/projects/{p['id']}/folders", json={"name": "Hilltops", "parent_id": sites["id"]}), 201)
    # cycle: Sites cannot move into its own child
    assert client.put(f"/api/folders/{sites['id']}", json={"name": "Sites", "parent_id": hill["id"]}).status_code == 422

    st = ok(client.post("/api/stations", json={"project_id": p["id"], "folder_id": hill["id"], "name": "Peak",
                                               "latitude": 47.25, "longitude": 9.34}), 201)
    assert st["project_id"] == p["id"] and st["folder_id"] == hill["id"]
    # Same station name is fine in another project, not in the same one.
    assert client.post("/api/stations", json={"project_id": p["id"], "name": "Peak", "latitude": 1, "longitude": 1}).status_code == 409
    other = ok(client.post("/api/projects", json={"name": "Other"}), 201)
    ok(client.post("/api/stations", json={"project_id": other["id"], "name": "Peak", "latitude": 1, "longitude": 1}), 201)

    ant = client.get("/api/antennas").json()[0]["id"]
    plot = ok(client.post("/api/plots", json={"project_id": p["id"], "folder_id": sites["id"], "name": "Peak-UHF",
                                              "antenna_id": ant, "tx_station_id": st["id"], "frequency_mhz": 446}), 201)
    assert plot["project_id"] == p["id"]
    # A plot cannot use a station from another project.
    foreign = client.get(f"/api/stations?project_id={other['id']}").json()[0]
    bad = client.post("/api/plots", json={"project_id": p["id"], "name": "Cross-UHF", "antenna_id": ant,
                                          "tx_station_id": foreign["id"], "frequency_mhz": 446})
    assert bad.status_code == 422

    # Move the plot to the root, the station to Sites.
    ok(client.patch(f"/api/plots/{plot['id']}/move", json={"folder_id": None}), 204)
    ok(client.patch(f"/api/stations/{st['id']}/move", json={"folder_id": sites["id"]}), 204)
    other_folder = ok(client.post(f"/api/projects/{other['id']}/folders", json={"name": "X"}), 201)
    assert client.patch(f"/api/stations/{st['id']}/move", json={"folder_id": other_folder["id"]}).status_code == 422

    tree = client.get(f"/api/projects/{p['id']}/tree").json()
    assert {f["name"] for f in tree["folders"]} == {"Sites", "Hilltops"}
    assert tree["stations"][0]["folder_id"] == sites["id"] and tree["plots"][0]["folder_id"] is None

    # Deleting Sites (which holds the station) while a plot outside uses it: refused.
    assert client.delete(f"/api/folders/{sites['id']}").status_code == 409
    # Put the plot inside too, then the whole folder (and Hilltops) goes.
    ok(client.patch(f"/api/plots/{plot['id']}/move", json={"folder_id": hill["id"]}), 204)
    ok(client.delete(f"/api/folders/{sites['id']}"), 204)
    tree = client.get(f"/api/projects/{p['id']}/tree").json()
    assert tree["folders"] == [] and tree["stations"] == [] and tree["plots"] == []

    ok(client.delete(f"/api/projects/{other['id']}"), 204)
    assert client.get(f"/api/stations?project_id={other['id']}").json() == []


def test_kml_import_and_project_export(client, worker):
    p = ok(client.post("/api/projects", json={"name": "KML project"}), 201)
    folder = ok(client.post(f"/api/projects/{p['id']}/folders", json={"name": "Imports"}), 201)
    res = ok(client.post(f"/api/projects/{p['id']}/layers",
                         data={"folder_id": str(folder["id"]), "create_stations": "true", "station_height_m": "15"},
                         files={"file": ("survey.kml", KML, "application/vnd.google-earth.kml+xml")}), 201)
    assert res["layer"]["name"] == "Survey" and res["layer"]["feature_count"] == 4
    assert res["stations_created"] == ["P1", "P2"] and res["stations_skipped"] == ["Far north"]
    assert res["layer"]["bounds"] == [9.3, 47.2, 20.0, 75.0]
    body = client.get(res["layer"]["url"]).content
    assert body == KML

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("doc.kml", KML)
    kmz = ok(client.post(f"/api/projects/{p['id']}/layers", files={"file": ("survey.kmz", buf.getvalue())}), 201)
    assert kmz["layer"]["kind"] == "kmz" and kmz["stations_created"] == []
    assert client.post(f"/api/projects/{p['id']}/layers", files={"file": ("x.txt", b"hi")}).status_code == 422
    assert client.post(f"/api/projects/{p['id']}/layers", files={"file": ("x.kml", b"<kml><bad")}).status_code == 422

    # Run a plot from an imported station, then export the project as one KMZ.
    stations = client.get(f"/api/stations?project_id={p['id']}").json()
    ant = client.get("/api/antennas").json()[0]["id"]
    plot = ok(client.post("/api/plots", json={"project_id": p["id"], "folder_id": folder["id"], "name": "P1-UHF",
                                              "antenna_id": ant, "tx_station_id": stations[0]["id"],
                                              "frequency_mhz": 446, "radius_km": 5}), 201)
    run = client.post(f"/api/plots/{plot['id']}/runs").json()
    assert worker.run_once()
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["status"] == "done", detail["error"]
    export = client.get(f"/api/projects/{p['id']}/export.kmz")
    assert export.status_code == 200 and "attachment" in export.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(export.content)) as zf:
        names = zf.namelist()
        doc = zf.read("doc.kml").decode()
    assert f"files/plot{plot['id']}_run{run['id']}.png" in names
    assert "<name>Imports</name>" in doc and "P1-UHF" in doc and "P2" in doc

    ok(client.delete(f"/api/layers/{kmz['layer']['id']}"), 204)
    assert client.get(kmz["layer"]["url"]).status_code == 404


def test_value_rasters_and_point_analysis(client, worker):
    p = ok(client.post("/api/projects", json={"name": "Values"}), 201)
    ant = client.get("/api/antennas").json()[0]["id"]
    st = ok(client.post("/api/stations", json={"project_id": p["id"], "name": "Tx", "latitude": 47.2, "longitude": 9.6}), 201)
    plot = ok(client.post("/api/plots", json={"project_id": p["id"], "name": "Values-UHF", "antenna_id": ant,
                                              "tx_station_id": st["id"], "frequency_mhz": 446, "radius_km": 5}), 201)
    run = client.post(f"/api/plots/{plot['id']}/runs").json()
    assert worker.run_once()
    d = client.get(f"/api/runs/{run['id']}").json()
    assert d["status"] == "done", d["error"]
    assert {"values.png", "values.tif", "coverage.png"} <= set(d["files"])
    assert (d["value_top"], d["value_step"], d["units"]) == (-30, -1, "dBm")
    assert d["values_url"].endswith("values.png")
    # The fake engine paints band 20 + 3 per pixel of distance from the centre
    # (-30 dBm - band); the exact centre lies on a pixel edge, so allow one pixel off.
    pt = client.get("/api/analysis/point", params={"lat": 47.2, "lon": 9.6, "run_ids": str(run["id"])}).json()
    assert pt["values"][0]["value"] in (-50, -53) and pt["values"][0]["unit"] == "dBm"
    far = client.get("/api/analysis/point", params={"lat": 10, "lon": 10, "run_ids": str(run["id"])}).json()
    assert far["values"][0]["value"] is None
    assert "measure.dcf" not in d["files"] and "coverage.ppm" not in d["files"]
    prof = client.get("/api/elevation/profile", params={"path": "9.5,47.1;9.7,47.3", "samples": 11}).json()
    assert len(prof["points"]) == 11 and 26000 < prof["distance_m"] < 28000
    assert client.get("/api/elevation/profile", params={"path": "9.5,47.1"}).status_code == 422


def test_copernicus_hd_and_buildings(client, worker, services, settings):
    p = ok(client.post("/api/projects", json={"name": "Surface models"}), 201)
    ant = client.get("/api/antennas").json()[0]["id"]
    st = ok(client.post("/api/stations", json={"project_id": p["id"], "name": "Roof", "latitude": 47.3, "longitude": 9.3}), 201)
    base = {"project_id": p["id"], "antenna_id": ant, "tx_station_id": st["id"], "frequency_mhz": 446, "radius_km": 3}
    bad = client.post("/api/plots", json={**base, "name": "Bad-HD", "resolution": 3600})
    assert bad.status_code == 422  # 3600 needs a 1" model
    hd = ok(client.post("/api/plots", json={**base, "name": "Cop-HD", "elevation_model": "cop30", "resolution": 3600}), 201)
    run = client.post(f"/api/plots/{hd['id']}/runs").json()
    assert worker.run_once()
    d = client.get(f"/api/runs/{run['id']}").json()
    assert d["status"] == "done", d["error"]
    assert d["command"][0].endswith("signalserverHD") and "-nothreads" in d["command"]
    assert (settings.sdf_dir_for("cop30") / "47_48_350_351-hd.sdf").exists()
    assert any("N47_00_E009_00" in c for c in services.calls)

    bld = ok(client.post("/api/plots", json={**base, "name": "Cop-OSM", "elevation_model": "cop30_buildings"}), 201)
    run = client.post(f"/api/plots/{bld['id']}/runs").json()
    assert worker.run_once()
    d = client.get(f"/api/runs/{run['id']}").json()
    assert d["status"] == "done", d["error"]
    assert (settings.sdf_dir_for("cop30_buildings") / "47_48_350_351.sdf").exists()
    summary = client.get("/api/elevation/summary").json()["models"]
    assert summary["cop30"]["tiles"] >= 1
    assert summary["cop30_buildings"]["buildings"] == 2 * 16  # mock returns 2 buildings per chunk
    # Terrain prefers the 1" grid: the mock's eastern half is 200 m higher.
    west = client.get("/api/elevation/height", params={"lat": 47.5, "lon": 9.2}).json()["height_m"]
    east = client.get("/api/elevation/height", params={"lat": 47.5, "lon": 9.8}).json()["height_m"]
    assert (west, east) == (500.0, 700.0)


def test_basemap_pack(client, worker, settings):
    est = client.get("/api/basemaps/estimate", params={"west": 9.3, "south": 47.38, "east": 9.45,
                                                       "north": 47.47, "max_zoom": 6}).json()
    assert est["ok"] and est["tiles"] == 1 + 4 + 16 + 64 + 256 + 1 + 1
    too_big = client.post("/api/basemaps", json={"name": "World", "west": -180, "south": -85, "east": 180,
                                                 "north": 85, "max_zoom": 15})
    assert too_big.status_code == 422
    pack = ok(client.post("/api/basemaps", json={"name": "St Gallen", "west": 9.3, "south": 47.38, "east": 9.45,
                                                 "north": 47.47, "max_zoom": 6, "with_elevation": False}), 202)
    assert pack["job"]["status"] == "queued"
    assert worker.run_once()
    packs = client.get("/api/basemaps").json()
    done = next(x for x in packs if x["id"] == pack["id"])
    assert done["ready"] and done["tile_count"] == est["tiles"] and done["job"]["status"] == "done", done
    assert done["source"] == "20260102.pmtiles"
    head = client.get(done["url"], headers={"Range": "bytes=0-6"})
    assert head.status_code == 206 and head.content == b"PMTiles"

    from pmtiles.reader import MmapSource, Reader

    with open(settings.basemap_dir / f"pack_{pack['id']}.pmtiles", "rb") as fh:
        r = Reader(MmapSource(fh))
        assert r.get(6, 33, 22) == b"tile-6-33-22"  # inside the area
        assert r.get(6, 0, 0) is None  # outside the area at zoom 6
        assert r.get(2, 3, 3) == b"tile-2-3-3"  # world context zooms
    ok(client.delete(f"/api/basemaps/{pack['id']}"), 204)
    assert not (settings.basemap_dir / f"pack_{pack['id']}.pmtiles").exists()
