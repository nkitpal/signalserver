"""Make sure Signal-Server has every SDF tile a plot needs.

Three elevation models are supported, each with its own SDF directory:

* ``dem3``: Viewfinder Panoramas DEM3, 3" (~90 m), mostly bare earth.
* ``cop30``: Copernicus GLO-30, 1" (~30 m) surface model (see copernicus.py).
* ``cop30_buildings``: GLO-30 with OpenStreetMap building heights burned in.

The 1" models can also produce "-hd" SDFs for signalserverHD (3600 px/degree).

For each DEM3 1x1 degree tile:

1. Already converted (row in ``elevation_tiles`` and SDF on disk)? Done.
2. HGT in the local cache? Convert it.
3. Otherwise download the Viewfinder Panoramas DEM3 archive(s) covering the
   tile, unpack every HGT inside (neighbouring tiles come for free) and convert.
4. If no archive covers the tile, or the archive has no HGT for it, the tile is
   open sea: write a sea-level SDF, because Signal-Server aborts on any
   missing tile.

Network failures raise ``ElevationError`` rather than silently writing sea.
"""

from __future__ import annotations

import fcntl
import logging
import os
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path

import httpx
from geoalchemy2.shape import from_shape
from shapely.geometry import box
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import ElevationArchive, ElevationTile
from .index import Archive, archives_for_tile, load_index
from . import copernicus
from .buildings import BuildingSource, BuildingsError, burn_buildings
from .tiles import (
    TileId,
    downsample_to_dem3,
    fill_voids,
    read_hgt,
    resample_to_dem3,
    write_sdf,
)

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

USER_AGENT = "signalserver-gui/2 (DEM3 tile fetcher)"


class ElevationError(RuntimeError):
    pass


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class ElevationProvider:
    def __init__(self, settings: Settings, session: Session, client: httpx.Client | None = None,
                 model: str = "dem3", buildings: BuildingSource | None = None):
        if model not in ("dem3", "cop30", "cop30_buildings"):
            raise ValueError(f"unknown elevation model {model!r}")
        self.settings = settings
        self.session = session
        self._client = client
        self.model = model
        self._buildings = buildings
        settings.ensure_dirs()

    @property
    def sdf_dir(self) -> Path:
        return self.settings.sdf_dir_for(self.model)

    # -- public -----------------------------------------------------------
    def ensure_tiles(self, tiles: Iterable[TileId], progress: ProgressFn | None = None,
                     hd: bool = False) -> Path:
        tiles = sorted(set(tiles))
        n = max(len(tiles), 1)
        for i, tile in enumerate(tiles):
            label = f"{self.model} {tile.hgt_name[:-4]}"
            if progress:
                progress(i / n, f"elevation {label}")
            sub = (lambda f, st, i=i: progress((i + f) / n, st)) if progress else None
            self.ensure_tile(tile, hd=hd, progress=sub)
        if progress:
            progress(1.0, "elevation ready")
        return self.sdf_dir

    def ensure_tile(self, tile: TileId, hd: bool = False,
                    progress: ProgressFn | None = None) -> ElevationTile:
        if hd and self.model == "dem3":
            raise ElevationError("The 3600 (HD) resolution needs a 1\" elevation model (Copernicus)")
        sdf_paths = [self.sdf_dir / n for n in tile.sdf_names(hd)]
        row = self._tile_row(tile)
        if row is not None and all(p.exists() for p in sdf_paths):
            return row
        with file_lock(self.sdf_dir / f".{tile.hgt_name}.lock"):
            row = self._tile_row(tile)
            if row is not None and all(p.exists() for p in sdf_paths):
                return row
            buildings = 0
            if self.model == "dem3":
                hgt = self._find_or_fetch_hgt(tile)
                grid = None if hgt is None else fill_voids(resample_to_dem3(read_hgt(hgt)))
                source = hgt.name if hgt else ""
                write_sdf(tile, grid, self.sdf_dir)
            else:
                grid1 = self._cop30_grid(tile)
                source = copernicus.tile_name(tile) if grid1 is not None else ""
                if grid1 is not None and self.model == "cop30_buildings":
                    grid1, buildings = self._with_buildings(tile, grid1, progress)
                    source += f" + {buildings} OSM buildings"
                write_sdf(tile, grid1, self.sdf_dir, hd=True) if hd else write_sdf(
                    tile, None if grid1 is None else downsample_to_dem3(grid1), self.sdf_dir
                )
                grid = grid1
            return self._record_tile(tile, grid, source, buildings)

    # -- Copernicus / buildings --------------------------------------------
    def _cop30_grid(self, tile: TileId):
        """1" GLO-30 grid for a tile (cached as a 3601x3601 HGT), or None for open sea."""
        path = self.settings.hgt1_dir / tile.hgt_name
        missing = self.settings.hgt1_dir / f"{tile.hgt_name}.none"
        if path.exists():
            return read_hgt(path)
        if missing.exists():
            return None
        if not self.settings.elevation_auto_download:
            raise ElevationError(
                f"Copernicus tile {tile.hgt_name} is not cached and automatic download is disabled."
            )
        url = copernicus.tile_url(tile).replace(copernicus.BUCKET_URL, self.settings.copernicus_url)
        with file_lock(self.settings.hgt1_dir / f".{tile.hgt_name}.lock"):
            if path.exists():
                return read_hgt(path)
            fd, tmp = tempfile.mkstemp(dir=self.settings.hgt1_dir, suffix=".tif")
            os.close(fd)
            try:
                log.info("Downloading %s", url)
                with open(tmp, "wb") as fh, self._http().stream("GET", url) as resp:
                    if resp.status_code in (403, 404):
                        missing.touch()  # no GLO-30 tile: open ocean
                        return None
                    if resp.status_code != 200:
                        raise ElevationError(f"Download of {url} failed: HTTP {resp.status_code}")
                    for chunk in resp.iter_bytes(1 << 20):
                        fh.write(chunk)
                grid = copernicus.read_cog(Path(tmp))
            except httpx.HTTPError as exc:
                raise ElevationError(f"Download of {url} failed: {exc}") from exc
            finally:
                os.unlink(tmp)
            copernicus.write_hgt(grid, path)
            return grid

    def _with_buildings(self, tile: TileId, grid, progress: ProgressFn | None):
        cached = self.settings.hgt1b_dir / tile.hgt_name
        count_file = self.settings.hgt1b_dir / f"{tile.hgt_name}.count"
        if cached.exists() and count_file.exists():
            return read_hgt(cached), int(count_file.read_text() or 0)
        source = self._buildings or BuildingSource(
            self.settings.osm_dir, default_height=self.settings.building_default_height_m,
            url=self.settings.overpass_url,
        )
        try:
            items = source.buildings(
                tile, (lambda f: progress(f, f"OSM buildings {tile.hgt_name[:-4]}")) if progress else None
            )
        except BuildingsError as exc:
            raise ElevationError(str(exc)) from exc
        raised, count = burn_buildings(grid, tile, items)
        copernicus.write_hgt(raised, cached)
        count_file.write_text(str(count))
        return raised, count

    def cached_hgt_tiles(self) -> list[TileId]:
        out = []
        for p in self.settings.hgt_dir.glob("*.hgt"):
            try:
                out.append(TileId.from_hgt_name(p.name))
            except ValueError:
                continue
        return sorted(out)

    # -- internals --------------------------------------------------------
    def _tile_row(self, tile: TileId) -> ElevationTile | None:
        return self.session.scalar(
            select(ElevationTile).where(
                ElevationTile.model == self.model, ElevationTile.lat == tile.lat, ElevationTile.lon == tile.lon
            )
        )

    def _record_tile(self, tile: TileId, grid, source: str, buildings: int = 0) -> ElevationTile:
        values = dict(
            model=self.model,
            building_count=buildings,
            lat=tile.lat,
            lon=tile.lon,
            has_data=grid is not None,
            source=source,
            min_elevation=int(grid.min()) if grid is not None else 0,
            max_elevation=int(grid.max()) if grid is not None else 0,
            footprint=from_shape(box(*tile.bounds), srid=4326),
        )
        stmt = insert(ElevationTile).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_elevation_tile",
            set_={k: stmt.excluded[k] for k in
                  ("has_data", "source", "min_elevation", "max_elevation", "building_count")},
        )
        self.session.execute(stmt)
        self.session.commit()
        return self._tile_row(tile)

    def _find_or_fetch_hgt(self, tile: TileId) -> Path | None:
        local = self.settings.hgt_dir / tile.hgt_name
        if local.exists():
            return local
        if not self.settings.elevation_auto_download:
            raise ElevationError(
                f"Elevation tile {tile.hgt_name} is not cached and automatic download is disabled."
            )
        index = load_index(str(self.settings.viewfinder_index_path) if self.settings.viewfinder_index_path else None)
        for archive in archives_for_tile(tile.lat, tile.lon, index):
            if self._archive_done(archive.name):
                if local.exists():
                    return local
                continue
            self._download_and_extract(archive)
            if local.exists():
                return local
        return None  # nothing covers it: open sea

    def _archive_done(self, name: str) -> bool:
        return self.session.get(ElevationArchive, name) is not None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.settings.elevation_download_timeout_s,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    def _download_and_extract(self, archive: Archive) -> None:
        with file_lock(self.settings.archive_dir / f".{archive.name}.lock"):
            self.session.expire_all()
            if self._archive_done(archive.name):
                return
            target = self.settings.archive_dir / f"{archive.name}.zip"
            if not target.exists():
                log.info("Downloading %s", archive.url)
                fd, tmp = tempfile.mkstemp(dir=self.settings.archive_dir, suffix=".part")
                try:
                    with os.fdopen(fd, "wb") as fh, self._http().stream("GET", archive.url) as resp:
                        if resp.status_code != 200:
                            raise ElevationError(
                                f"Download of {archive.url} failed: HTTP {resp.status_code}"
                            )
                        for chunk in resp.iter_bytes(1 << 20):
                            fh.write(chunk)
                    os.replace(tmp, target)
                except httpx.HTTPError as exc:
                    raise ElevationError(f"Download of {archive.url} failed: {exc}") from exc
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
            count = self._extract(target)
            size = target.stat().st_size
            if not self.settings.elevation_keep_archives:
                target.unlink(missing_ok=True)
            self.session.merge(
                ElevationArchive(name=archive.name, url=archive.url, size_bytes=size, tile_count=count)
            )
            self.session.commit()

    def _extract(self, zip_path: Path) -> int:
        count = 0
        try:
            zf = zipfile.ZipFile(zip_path)
        except zipfile.BadZipFile as exc:
            zip_path.unlink(missing_ok=True)
            raise ElevationError(f"{zip_path.name} is not a valid zip archive") from exc
        with zf:
            for info in zf.infolist():
                base = os.path.basename(info.filename)
                if info.is_dir() or not base.lower().endswith(".hgt"):
                    continue
                try:
                    tile = TileId.from_hgt_name(base)
                except ValueError:
                    continue
                dest = self.settings.hgt_dir / tile.hgt_name
                fd, tmp = tempfile.mkstemp(dir=self.settings.hgt_dir, suffix=".part")
                with os.fdopen(fd, "wb") as out, zf.open(info) as src:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
                os.replace(tmp, dest)
                count += 1
        log.info("Unpacked %d HGT tiles from %s", count, zip_path.name)
        return count
