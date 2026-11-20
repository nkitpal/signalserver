"""Application settings, read from environment variables (prefix ``SSG_``) or ``.env``."""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SSG_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://signalserver:signalserver@localhost:5432/signalserver"
    data_dir: Path = Path("data")

    # Signal-Server binaries. signalserverHD / signalserverLIDAR are expected next to it.
    signalserver_path: str = "signalserver"
    engine_timeout_s: int = 1800

    # Elevation (Viewfinder Panoramas DEM3)
    elevation_auto_download: bool = True
    elevation_keep_archives: bool = False
    elevation_download_timeout_s: int = 600
    viewfinder_index_path: Path | None = None

    # Worker
    worker_poll_interval_s: float = 1.0
    worker_stale_after_s: int = 300

    # Web / viewer
    host: str = "127.0.0.1"
    port: int = 8080
    cors_origins: list[str] = Field(default_factory=list)
    frontend_dist: Path = Path("frontend/dist")
    cesium_ion_token: str = ""
    imagery_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    imagery_credit: str = "© OpenStreetMap contributors"
    satellite_url: str = (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
    )
    satellite_credit: str = "Imagery © Esri, Maxar, Earthstar Geographics"

    # Derived directories -------------------------------------------------
    @property
    def elevation_dir(self) -> Path:
        return self.data_dir / "elevation"

    @property
    def hgt_dir(self) -> Path:
        return self.elevation_dir / "hgt"

    @property
    def sdf_dir(self) -> Path:
        return self.elevation_dir / "sdf"

    @property
    def archive_dir(self) -> Path:
        return self.elevation_dir / "archives"

    @property
    def antenna_dir(self) -> Path:
        return self.data_dir / "antennas"

    @property
    def color_dir(self) -> Path:
        return self.data_dir / "color_profiles"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    def engine_binary(self, resolution: int | None) -> str:
        base = self.signalserver_path
        return base + "HD" if resolution == 3600 else base

    def engine_available(self) -> bool:
        return shutil.which(self.signalserver_path) is not None

    def ensure_dirs(self) -> None:
        for d in (self.hgt_dir, self.sdf_dir, self.archive_dir, self.antenna_dir,
                  self.color_dir, self.runs_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
