# Signal Server GUI

A web app for planning RF coverage with [Signal-Server](https://github.com/W3AXL/Signal-Server). You
place stations on a 3D globe and define plots (frequency, power, antenna, propagation model).
Each run produces a coverage map that loads straight onto the globe. Elevation data is
downloaded automatically from [Viewfinder Panoramas](https://www.viewfinderpanoramas.org/dem3.html).

![architecture](https://img.shields.io/badge/stack-FastAPI%20%7C%20PostGIS%20%7C%20CesiumJS-4da3ff)

## Features

- **3D globe (CesiumJS)**: terrain, street and satellite base maps, fly-to navigation, click to
  place stations. Coverage overlays have adjustable opacity and a legend. Point-to-point runs
  add the path line, obstruction points and a terrain/Fresnel profile chart.
- **Automatic elevation data**: before each run, the app works out which 1°×1° tiles the plot
  needs and fetches any missing ones. It downloads the Viewfinder Panoramas DEM3 archive
  (3″, ~90 m), converts its HGT files to Signal-Server's SDF format and caches the result.
  Ocean tiles become sea-level tiles, since Signal-Server aborts on a missing tile. Signal-Server
  names tiles near the 0° meridian differently, and the app creates those names too. The globe's
  terrain is drawn from the same tiles, so what you see matches what the propagation model uses.
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
| `elevation/sdf/` | Signal-Server SDF tiles |
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

## Notes and limits

- DEM3 is 3 arc-second data, so plots use 300, 600 or 1200 pixels per degree. The 3600 (HD)
  resolution needs 1″ data and is not offered.
- Signal-Server supports transmitter latitudes up to ±70°.
- Please respect Viewfinder Panoramas' terms. The data is free, but each archive is only
  downloaded once and then cached.

## License

GPL-3.0. Signal-Server itself is GPL-2.0. Elevation data © Jonathan de Ferranti, Viewfinder
Panoramas.
