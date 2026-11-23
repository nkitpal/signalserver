# Signal Server GUI

A web app for planning RF coverage with [Signal-Server](https://github.com/W3AXL/Signal-Server). You
organise stations and coverage plots in projects and folders, run them in the background, and
explore the results on a 3D globe: compare several transmitters pixel by pixel, measure
distances and areas, import KML, and work offline. Elevation data (terrain, or a 30 m surface
model with buildings) is downloaded automatically.

![architecture](https://img.shields.io/badge/stack-FastAPI%20%7C%20PostGIS%20%7C%20CesiumJS-4da3ff)

## Features

- **Projects and folders**: each project holds a folder tree (like a file explorer) of stations,
  plots and imported layers. Tick folders or items to show or hide them on the globe, drag items
  between folders, and export a whole project as one KMZ for Google Earth.
- **3D globe (CesiumJS)**: terrain, street and satellite base maps, fly-to navigation, click to
  place stations. Coverage overlays have adjustable opacity and a legend. Point-to-point runs
  add the path line, obstruction points and a terrain/Fresnel profile chart.
- **Compare several plots** (Analyze tab): hover anywhere to see every visible plot's value at
  that point, strongest first. Composite views:
  - *Best server*: each pixel takes the colour of the plot with the strongest signal.
  - *Overlap count*: how many plots cover a pixel, optionally above a threshold.
  - *Combined coverage*: the strongest signal of any plot, drawn in one palette.
- **Real values per pixel**: Signal-Server only writes a coloured image, so each run uses a
  128-band measurement palette (1 dB steps, 2 dB for path loss) and the image is decoded back
  into values. Each run stores them as `values.tif` (Int16 GeoTIFF for GIS) and `values.png`
  (for the browser). The display image is then coloured with your palette.
- **Three elevation models**, chosen per plot:
  - *DEM3*: Viewfinder Panoramas, 3″ (~90 m), mostly bare terrain.
  - *Copernicus GLO-30*: 1″ (~30 m) surface model that partly includes forests and buildings.
    This model also enables the 3600 ppd (HD) resolution.
  - *Copernicus + OpenStreetMap buildings*: OSM building footprints with their height (or
    levels × 3 m) are added on top of GLO-30.

  Tiles are downloaded, converted to Signal-Server's SDF format and cached when a plot first
  needs them. Ocean tiles become sea-level tiles, and Signal-Server's tile names near the 0°
  meridian are handled. The globe's terrain is drawn from the same tiles.
- **Measuring tools**: geodesic distance (with segments and bearing), area, and a terrain
  profile between two points.
- **Import KML/KMZ** as layers (styles kept). Placemark points can optionally become stations.
- **Google Earth**: *Open in Google Earth* downloads a plot's KMZ and opens Google Earth Web at
  the plot's location. To load it there, use Projects → New project → Import KML file. Google
  Earth can't load a KMZ from a URL.
- **Offline maps**: download a region of the OpenStreetMap street map (Protomaps vector tiles,
  stored as PMTiles) plus its elevation data. Then choose *Offline streets* to use the app
  without internet. The browser draws the map itself, so no image tiles are stored.
- **PostgreSQL + PostGIS**: stations, coverage footprints and elevation tiles are spatial
  columns with GiST indexes. Each run stores a snapshot of its inputs, so results stay
  reproducible after the plot is edited.
- **Background jobs**: a job queue stored in PostgreSQL (`FOR UPDATE SKIP LOCKED`), with no
  Redis needed. Run as many workers as you like.
- **Outputs per run**: transparent PNG, Cloud-Optimized GeoTIFF (EPSG:4326), Google Earth KMZ,
  a path report as JSON and text, and a zip of everything.
- **All Signal-Server propagation models**: ITM/Longley-Rice, ITWOM, Hata, COST231, ECC33,
  SUI, Ericsson, Egli, FSPL, plane earth and others, with terrain, climate and clutter options.

## Architecture

```
 Browser (Vite + TypeScript + CesiumJS)
        │  REST /api/*, coverage PNGs, terrain heightmaps
        ▼
 FastAPI app ──────────────┐            PostgreSQL + PostGIS
   signalserver-gui serve  │──────────▶ stations, antennas, plots, plot_runs,
        │ enqueue job      │            jobs, elevation_tiles, elevation_archives
        ▼                  │
 Worker(s) ────────────────┘
   signalserver-gui worker
        │ 1. ensure DEM tiles ──▶ viewfinderpanoramas.org (DEM3 zips) → HGT → SDF cache
        │ 2. run signalserver   ──▶ PPM + bounds (+ P2P report)
        └ 3. write PNG / COG / KMZ / JSON into data/runs/<id>/
```

Data directory layout (`SSG_DATA_DIR`, default `./data`):

| Path | Contents |
| --- | --- |
| `elevation/hgt/` | unpacked Viewfinder HGT tiles (also used for globe terrain) |
| `elevation/hgt1/`, `hgt1b/` | Copernicus GLO-30 tiles as 3601×3601 grids (without / with OSM buildings) |
| `elevation/osm/` | cached Overpass building responses |
| `elevation/sdf/`, `sdf-cop30/`, `sdf-cop30b/` | Signal-Server SDF tiles per elevation model |
| `basemaps/` | offline street-map packs (`pack_<id>.pmtiles`) |
| `layers/` | imported KML/KMZ files |
| `elevation/archives/` | downloaded zips (deleted after unpacking unless `SSG_ELEVATION_KEEP_ARCHIVES=true`) |
| `antennas/<category>/` | `.ant` patterns and their `.az`/`.el` conversions |
| `color_profiles/` | `.dcf/.scf/.lcf` palettes |
| `runs/<run id>/` | outputs of each run |

## Quick start with Docker

```bash
docker compose up -d --build
```

Then open <http://localhost:8080>. Compose starts PostGIS, a one-off `migrate` job, the web/API
service and one worker. To run several plots in parallel:

```bash
docker compose up -d --scale worker=3
```

The image builds Signal-Server from source (W3AXL fork, pinned commit), so no separate install
is needed. Optional settings go in a `.env` file next to `docker-compose.yml`:

```
POSTGRES_PASSWORD=change-me
PORT=8080
CESIUM_ION_TOKEN=            # optional: Cesium World Terrain instead of the local DEM terrain
ELEVATION_AUTO_DOWNLOAD=true
```

## Native install (Debian/Ubuntu)

```bash
scripts/install-native.sh --with-system-packages --create-db
```

```bash
.venv/bin/signalserver-gui serve --with-worker
```

The script installs the build tools, PostgreSQL/PostGIS and Node packages, and builds
Signal-Server into `./.engine`. It also creates the Python venv, builds the frontend, writes
`.env` and applies the database migrations. Drop the flags you don't need, for example
`--with-system-packages` if the tools are already installed or `--create-db` if you already
have a PostGIS database.

For larger setups, run the web server and the workers as separate processes:

```bash
.venv/bin/signalserver-gui serve
```

```bash
.venv/bin/signalserver-gui worker
```

## Configuration

Settings come from environment variables or `.env` (prefix `SSG_`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `SSG_DATABASE_URL` | `postgresql+psycopg://signalserver:signalserver@localhost:5432/signalserver` | PostGIS database |
| `SSG_DATA_DIR` | `data` | elevation cache, antennas, palettes, run outputs |
| `SSG_SIGNALSERVER_PATH` | `signalserver` | engine binary; `signalserverHD` and `signalserverLIDAR` must sit next to it |
| `SSG_ELEVATION_AUTO_DOWNLOAD` | `true` | fetch missing DEM tiles from Viewfinder Panoramas |
| `SSG_ELEVATION_KEEP_ARCHIVES` | `false` | keep downloaded zips |
| `SSG_VIEWFINDER_INDEX_PATH` | bundled | archive index override (see `elevation refresh-index`) |
| `SSG_COPERNICUS_URL` | AWS open data bucket | Copernicus GLO-30 source (mirror it for air-gapped setups) |
| `SSG_OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | OpenStreetMap buildings source |
| `SSG_BUILDING_DEFAULT_HEIGHT_M` | `6` | height for OSM buildings without height/levels tags |
| `SSG_BASEMAP_SOURCE_URL`, `SSG_BASEMAP_BUILDS_URL` | Protomaps daily builds | offline map source |
| `SSG_BASEMAP_MAX_TILES` | `400000` | size limit for one offline map pack |
| `SSG_CESIUM_ION_TOKEN` | empty | use Cesium World Terrain on the globe |
| `SSG_IMAGERY_URL`, `SSG_SATELLITE_URL` | OSM, Esri World Imagery | base map tile templates (`{z}/{x}/{y}`) |
| `SSG_HOST`, `SSG_PORT` | `127.0.0.1`, `8080` | web server bind address |

## Command line

```
signalserver-gui serve [--with-worker]          web UI + API
signalserver-gui worker                         process queued runs and downloads
signalserver-gui init                           migrate + install bundled antennas/palettes
signalserver-gui elevation fetch W S E N        pre-download a region (e.g. 5 45 11 48 for the Alps)
signalserver-gui elevation import PATH...       import local .hgt files (offline installs)
signalserver-gui elevation refresh-index        re-read the Viewfinder coverage map
```

You can also pre-download elevation for the current globe view from the **Elevation** tab.

## API

Interactive docs are served at `/docs`. Main endpoints:

- `GET/POST /api/projects`, `PUT/DELETE /api/projects/{id}`, `GET /api/projects/{id}/tree`,
  `GET /api/projects/{id}/export.kmz`
- `POST /api/projects/{id}/folders`, `PUT/DELETE /api/folders/{id}`, and
  `PATCH /api/{stations|plots|layers}/{id}/move` to move items between folders.
- `POST /api/projects/{id}/layers` (KML/KMZ upload, `create_stations=true` to turn points into
  stations), `GET /api/layers/{id}/file`, `DELETE /api/layers/{id}`
- `GET /api/analysis/point?lat=&lon=&run_ids=1,2` returns several runs' values at a point,
  strongest first. `GET /api/elevation/profile?path=lon,lat;lon,lat` returns a terrain profile.
- `GET/POST /api/basemaps` (offline map packs), `GET /api/basemaps/estimate`,
  `GET /api/basemaps/{id}.pmtiles` (supports HTTP Range requests)
- `GET/POST /api/stations`, `GET/PUT/DELETE /api/stations/{id}`, `GET /api/stations.geojson`
- `GET/POST /api/antennas` (multipart `.ant` upload), `PATCH/DELETE /api/antennas/{id}`
- `GET/POST /api/plots`, `GET/PUT/DELETE /api/plots/{id}`
- `POST /api/plots/{id}/runs` queues a run. `GET /api/runs?west=&south=&east=&north=` does a
  spatial search on footprints, `GET /api/runs/{id}` returns a run and
  `GET /api/runs/{id}/files/{name}` serves its files.
- `GET /api/elevation/summary`, `tiles.geojson`, `archives`, `height?lat=&lon=`,
  `POST /api/elevation/prefetch`
- `GET /api/elevation/terrain/{level}/{x}/{y}.f32` serves the globe's heightmaps.

## Development

```bash
pip install -e ".[dev]"
```

```bash
cd frontend && npm ci
```

Run the API (auto-reload), a worker and the Vite dev server (which proxies `/api` to
`127.0.0.1:8080`) in three terminals. `--reload` doesn't start the in-process worker:

```bash
signalserver-gui serve --reload
```

```bash
signalserver-gui worker
```

```bash
cd frontend && npm run dev
```

Run the unit tests. Unit tests that need no database run anywhere; the API and worker tests
run when `SSG_TEST_DATABASE_URL` is set and wipe that database:

```bash
pytest
```

```bash
SSG_TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost/signalserver_test pytest
```

For the end-to-end test, start a server with a worker and real engine, then run:

```bash
cd frontend && SSG_E2E_URL=http://127.0.0.1:8080 npx playwright test
```

## Outgoing network access

The server (not the browser) needs these hosts. When they're unreachable, use only cached data,
offline packs and `elevation import`.

| Host | Used for |
| --- | --- |
| `viewfinderpanoramas.org` | DEM3 elevation |
| `copernicus-dem-30m.s3.amazonaws.com` | Copernicus GLO-30 elevation |
| `overpass-api.de` | OpenStreetMap buildings |
| `build.protomaps.com`, `build-metadata.protomaps.dev` | offline street maps |

The browser loads base-map tiles from OpenStreetMap and Esri unless you use *Offline streets*.

## Notes and limits

- The 3600 ppd (HD, 30 m) resolution needs a Copernicus model. It is marked *experimental*:
  upstream `signalserverHD` can crash when multithreaded (it always runs single-threaded here),
  and it sometimes draws wedge-shaped artefacts that the 1200 ppd run of the same data doesn't.
  HD plots can use at most 32 1°×1° tiles; standard plots can use 100.
- Value rasters are exact to the engine's 1 dB (2 dB for path loss). Signal stronger than
  −30 dBm (130 dBµV/m) is reported as that cap.
- Building heights are only as good as OpenStreetMap's tags. Buildings without `height` or
  `building:levels` get `SSG_BUILDING_DEFAULT_HEIGHT_M`. Large cities take a few minutes the
  first time (16 Overpass requests per 1°×1° tile; the responses are cached).
- Signal-Server supports transmitter latitudes up to ±70°.
- Please respect Viewfinder Panoramas' terms. The data is free, but each archive is only
  downloaded once and then cached.

## License

GPL-3.0. Signal-Server itself is GPL-2.0. Elevation data © Jonathan de Ferranti, Viewfinder
Panoramas.
