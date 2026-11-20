#!/usr/bin/env python3
"""Stand-in for the Signal-Server binary used by the integration tests.

It checks that the SDF tile for the transmitter exists (proving the elevation
provider ran), writes a small PPM, prints the "Area boundaries" line and, for
point-to-point runs, writes the report and profile series.
"""

import math
import os
import shutil
import sys

args = sys.argv[1:]


def opt(flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default


lat, lon = float(opt("-lat")), float(opt("-lon"))
radius = float(opt("-R"))
if "-m" not in args:
    radius /= 0.621371
out = opt("-o")
if len(os.path.basename(out)) < 5:
    print("[error] Output name is too short. Must be at least 5 chars")
    sys.exit(1)

sdf_dir = opt("-sdf")
west = (-(math.floor(lon) + 1)) % 360
tile = f"{math.floor(lat)}_{math.floor(lat) + 1}_{west}_{west + 1}.sdf"
if not os.path.exists(os.path.join(sdf_dir, tile)):
    print(f"[error] missing tile {tile}")
    sys.exit(2)

if "-rla" in args:
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "p2p_report.txt")
    shutil.copyfile(fixture, out + ".txt")
    for name in ("profile", "reference", "curvature", "fresnel", "fresnel60"):
        with open(f"{out}_{name}", "w") as fh:
            for i in range(10):
                fh.write(f"{i * 0.5:.3f} {100 + i:.3f}\n")
    sys.exit(0)

w, h = 40, 30
with open(out + ".ppm", "wb") as fh:
    fh.write(f"P6\n{w} {h}\n255\n".encode())
    for y in range(h):
        for x in range(w):
            inside = (x - w / 2) ** 2 + (y - h / 2) ** 2 < 100
            fh.write(bytes((0, 200, 0) if inside else (255, 255, 255)))
dlat = radius / 111.2
dlon = dlat / math.cos(math.radians(lat))
print(f"[info] Area boundaries:{lat + dlat:.6f} | {lon + dlon:.6f} | {lat - dlat:.6f} | {lon - dlon:.6f} ")
