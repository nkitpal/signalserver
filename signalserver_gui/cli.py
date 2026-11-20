"""Command line entry point: ``signalserver-gui <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import get_settings


def _alembic_config(url: str | None = None):
    from alembic.config import Config

    here = Path(__file__).parent / "migrations"
    cfg = Config(str(here / "alembic.ini"))
    cfg.set_main_option("script_location", str(here))
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def migrate(url: str | None = None) -> None:
    """Create the PostGIS extension (if allowed) and apply migrations."""
    from alembic import command
    from sqlalchemy import create_engine, text

    url = url or get_settings().database_url
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    engine.dispose()
    command.upgrade(_alembic_config(url), "head")


def seed() -> None:
    """Install bundled antenna patterns/palettes and register the antennas."""
    from sqlalchemy import select

    from .db import session_scope
    from .engine.patterns import install_bundled
    from .models import Antenna

    s = get_settings()
    s.ensure_dirs()
    bundled = install_bundled(s.antenna_dir, s.color_dir)
    names = {"dipole": "Generic Dipole", "yagi": "Generic Yagi", "cardio": "Generic Cardioid",
             "ellipse": "Generic Ellipse", "corner": "Generic Corner Reflector"}
    with session_scope() as db:
        existing = {(a.category, a.pattern) for a in db.scalars(select(Antenna))}
        for category, pattern in bundled:
            if (category, pattern) in existing:
                continue
            db.add(Antenna(name=names.get(pattern, pattern), category=category, pattern=pattern,
                           description="Bundled pattern"))
    print(f"Bundled antennas available: {len(bundled)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="signalserver-gui")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the web server")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--with-worker", action="store_true",
                         help="also process jobs in this process (single-machine setups)")
    p_serve.add_argument("--reload", action="store_true")

    sub.add_parser("worker", help="process queued plot runs and elevation downloads")
    sub.add_parser("migrate", help="create/upgrade the database schema")
    sub.add_parser("seed", help="install bundled antennas and colour palettes")
    sub.add_parser("init", help="migrate + seed")

    p_el = sub.add_parser("elevation", help="elevation data tools")
    el = p_el.add_subparsers(dest="el_cmd", required=True)
    p_fetch = el.add_parser("fetch", help="download and convert tiles for a bounding box")
    p_fetch.add_argument("west", type=float)
    p_fetch.add_argument("south", type=float)
    p_fetch.add_argument("east", type=float)
    p_fetch.add_argument("north", type=float)
    p_idx = el.add_parser("refresh-index", help="re-read the Viewfinder coverage map")
    p_idx.add_argument("--output", type=Path, help="where to write the index (default: data dir)")
    p_imp = el.add_parser("import", help="import local .hgt files (e.g. from an offline copy)")
    p_imp.add_argument("paths", nargs="+", type=Path)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if args.cmd == "serve":
        import uvicorn

        from .api.app import create_app

        if args.reload:
            uvicorn.run("signalserver_gui.api.asgi:app", host=args.host or settings.host,
                        port=args.port or settings.port, reload=True)
        else:
            uvicorn.run(create_app(settings, with_worker=args.with_worker),
                        host=args.host or settings.host, port=args.port or settings.port)
    elif args.cmd == "worker":
        from .worker import Worker

        Worker(settings).run_forever()
    elif args.cmd == "migrate":
        migrate()
    elif args.cmd == "seed":
        seed()
    elif args.cmd == "init":
        migrate()
        seed()
    elif args.cmd == "elevation":
        _elevation(args, settings)


def _elevation(args, settings) -> None:
    from .db import session_scope
    from .elevation.provider import ElevationProvider
    from .elevation.tiles import TileId, tiles_for_bbox

    if args.el_cmd == "fetch":
        tiles = tiles_for_bbox(args.west, args.south, args.east, args.north)
        print(f"{len(tiles)} tile(s)")
        with session_scope() as db:
            ElevationProvider(settings, db).ensure_tiles(
                tiles, lambda f, s: print(f"  [{f:5.0%}] {s}", flush=True))
    elif args.el_cmd == "refresh-index":
        import httpx

        from .elevation.index import COVERAGE_PAGE_URL, parse_coverage_page, save_index

        html = httpx.get(COVERAGE_PAGE_URL, timeout=60, follow_redirects=True).text
        archives = parse_coverage_page(html)
        if len(archives) < 100:
            sys.exit(f"Only {len(archives)} archives found; page layout may have changed.")
        out = args.output or settings.elevation_dir / "viewfinder_dem3_index.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        save_index(archives, out)
        print(f"Wrote {len(archives)} archives to {out}; set SSG_VIEWFINDER_INDEX_PATH={out}")
    elif args.el_cmd == "import":
        import shutil

        settings.ensure_dirs()
        n = 0
        imported = []
        for p in args.paths:
            for f in ([p] if p.is_file() else sorted(p.rglob("*.hgt"))):
                try:
                    tile = TileId.from_hgt_name(f.name)
                except ValueError:
                    continue
                shutil.copyfile(f, settings.hgt_dir / tile.hgt_name)
                imported.append(tile)
                n += 1
        # Force re-conversion of tiles that were prepared before (e.g. as sea level).
        from sqlalchemy import delete

        from .models import ElevationTile

        with session_scope() as db:
            for tile in imported:
                db.execute(delete(ElevationTile).where(ElevationTile.lat == tile.lat,
                                                       ElevationTile.lon == tile.lon))
                for name in tile.sdf_names():
                    (settings.sdf_dir / name).unlink(missing_ok=True)
        print(f"Imported {n} HGT tile(s); they are converted when first needed.")


if __name__ == "__main__":
    main()
