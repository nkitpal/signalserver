#!/usr/bin/env bash
# Native (non-Docker) install for Debian/Ubuntu.
#
#   scripts/install-native.sh [--with-system-packages] [--engine-prefix DIR] [--create-db]
#
#   --with-system-packages  apt-get install build tools, PostgreSQL + PostGIS, Node.js deps (uses sudo)
#   --engine-prefix DIR     where to install the Signal-Server binaries (default: ./.engine)
#   --create-db             create the 'signalserver' role/database with PostGIS (uses sudo -u postgres)
#
# Afterwards:  .venv/bin/signalserver-gui serve --with-worker   ->  http://127.0.0.1:8080
set -euo pipefail
cd "$(dirname "$0")/.."

WITH_PACKAGES=0
CREATE_DB=0
ENGINE_PREFIX="$PWD/.engine"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-system-packages) WITH_PACKAGES=1 ;;
    --create-db) CREATE_DB=1 ;;
    --engine-prefix) ENGINE_PREFIX="$2"; shift ;;
    -h|--help) sed -n 2,12p "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [[ $WITH_PACKAGES == 1 ]]; then
  step "Installing system packages"
  sudo apt-get update
  sudo apt-get install -y git cmake g++ make libbz2-dev zlib1g-dev libspdlog-dev \
    python3 python3-venv python3-pip postgresql postgis nodejs npm
fi

for cmd in git cmake g++ python3 npm; do
  command -v "$cmd" >/dev/null || { echo "Missing '$cmd' (re-run with --with-system-packages)" >&2; exit 1; }
done
python3 - <<'PY' || { echo "Python 3.11+ is required" >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

if [[ ! -x "$ENGINE_PREFIX/bin/signalserver" ]]; then
  step "Building Signal-Server into $ENGINE_PREFIX"
  scripts/build-signalserver.sh "$ENGINE_PREFIX"
fi

step "Python environment"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -e ".[dev]"

step "Frontend build"
(cd frontend && npm ci --no-audit --no-fund && npm run build)

if [[ $CREATE_DB == 1 ]]; then
  step "Creating PostgreSQL database 'signalserver'"
  sudo -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'signalserver') THEN
    CREATE ROLE signalserver LOGIN PASSWORD 'signalserver';
  END IF;
END $$;
SQL
  sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='signalserver'" | grep -q 1 \
    || sudo -u postgres createdb -O signalserver signalserver
  sudo -u postgres psql -d signalserver -c "CREATE EXTENSION IF NOT EXISTS postgis"
fi

if [[ ! -f .env ]]; then
  step "Writing .env"
  cat > .env <<EOF
SSG_DATABASE_URL=postgresql+psycopg://signalserver:signalserver@localhost:5432/signalserver
SSG_DATA_DIR=$PWD/data
SSG_SIGNALSERVER_PATH=$ENGINE_PREFIX/bin/signalserver
# SSG_CESIUM_ION_TOKEN=   # optional: Cesium World Terrain on the globe
EOF
fi

step "Database schema and bundled antennas"
.venv/bin/signalserver-gui init

step "Done"
echo "Start with:  .venv/bin/signalserver-gui serve --with-worker"
echo "Then open:   http://127.0.0.1:8080"
