#!/usr/bin/env bash
# Build Signal-Server (W3AXL fork, the maintained successor of Cloud-RF/Signal-Server)
# and install signalserver, signalserverHD and signalserverLIDAR into PREFIX/bin.
#
# Usage: scripts/build-signalserver.sh [PREFIX]      (default PREFIX: /usr/local)
# Needs: git, cmake, g++, libbz2-dev, zlib1g-dev, libspdlog-dev (Debian/Ubuntu package names)
set -euo pipefail

PREFIX="${1:-/usr/local}"
REPO="${SIGNALSERVER_REPO:-https://github.com/W3AXL/Signal-Server.git}"
REF="${SIGNALSERVER_REF:-7f6242a}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone --quiet "$REPO" "$WORK/src"
git -C "$WORK/src" checkout --quiet "$REF"
# Keep upstream compiler flags (-O2): a CMAKE_BUILD_TYPE=Release (-O3) build segfaults while
# loading tiles.
cmake -S "$WORK/src/src" -B "$WORK/build" >/dev/null
cmake --build "$WORK/build" -j "$(nproc)"
mkdir -p "$PREFIX/bin"
install -m 0755 "$WORK/build/signalserver" "$WORK/build/signalserverHD" "$WORK/build/signalserverLIDAR" "$PREFIX/bin/"
echo "Installed Signal-Server ($REF) to $PREFIX/bin"
