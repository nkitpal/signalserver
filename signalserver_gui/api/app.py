"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings, get_settings
from ..engine.patterns import list_palettes
from . import antennas, basemaps, elevation, localize, projects, resources, runs

log = logging.getLogger(__name__)

PROPAGATION_MODELS = {
    1: "ITM (Longley-Rice)", 2: "Line of sight", 3: "Okumura-Hata", 4: "ECC33 (ITU-R P.529)",
    5: "SUI microwave", 6: "COST231-Hata", 7: "Free space path loss", 8: "ITWOM 3.0",
    9: "Ericsson", 10: "Plane earth", 11: "Egli VHF/UHF", 12: "Soil",
}


def create_app(settings: Settings | None = None, with_worker: bool = False) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if with_worker:
            from ..worker import start_background_worker

            app.state.worker = start_background_worker(settings)
        yield
        if with_worker:
            app.state.worker.stop()

    app = FastAPI(title="Signal Server GUI", version=__version__, lifespan=lifespan)
    app.dependency_overrides[get_settings] = lambda: settings
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                           allow_methods=["*"], allow_headers=["*"])

    app.include_router(projects.router)
    app.include_router(antennas.router)
    app.include_router(localize.router)
    app.include_router(basemaps.router)
    app.include_router(resources.router)
    app.include_router(runs.router)
    app.include_router(elevation.router)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/config")
    def viewer_config(s: Settings = Depends(get_settings)):
        return {
            "version": __version__,
            "engine_available": s.engine_available(),
            "engine_path": s.signalserver_path,
            "elevation_auto_download": s.elevation_auto_download,
            "cesium_ion_token": s.cesium_ion_token,
            "imagery": [
                {"id": "osm", "name": "Streets", "url": s.imagery_url, "credit": s.imagery_credit},
                {"id": "satellite", "name": "Satellite", "url": s.satellite_url, "credit": s.satellite_credit},
            ],
            "palettes": list_palettes(s.color_dir),
            "elevation_models": {
                "dem3": "DEM3 90 m (terrain)",
                "cop30": "Copernicus 30 m (surface)",
                "cop30_buildings": "Copernicus 30 m + OSM buildings",
            },
            "propagation_models": PROPAGATION_MODELS,
        }

    dist = settings.frontend_dist
    if dist.is_dir() and (dist / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
        if (dist / "cesium").is_dir():
            app.mount("/cesium", StaticFiles(directory=dist / "cesium"), name="cesium")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                from fastapi import HTTPException

                raise HTTPException(404)
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and dist.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
    else:
        log.warning("Frontend build not found at %s; serving API only", dist)

    return app
