#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import math
import time
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
KNOWN_BLOCKED_TILE_SHA1 = {
    # OpenStreetMap public tile server "Access blocked" image.
    "0cfb5f443183efc5921f61005aaa7f341fcfd143",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download slippy-map tiles for offline YP ground station use.")
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("WEST", "SOUTH", "EAST", "NORTH"), required=True)
    parser.add_argument("--zoom-min", type=int, required=True)
    parser.add_argument("--zoom-max", type=int, required=True)
    parser.add_argument("--out", type=Path, default=Path("data/tiles"))
    parser.add_argument("--url-template", help="XYZ raster tile URL, for example https://tiles.example.com/{z}/{x}/{y}.png")
    parser.add_argument("--sleep", type=float, default=0.25, help="Delay between tile requests.")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing cached tiles.")
    parser.add_argument(
        "--allow-osm-public-tiles",
        action="store_true",
        help="Allow the public tile.openstreetmap.org endpoint. This is not suitable for bulk/offline preloading.",
    )
    args = parser.parse_args()

    url_template = args.url_template or DEFAULT_URL
    if "tile.openstreetmap.org" in url_template and not args.allow_osm_public_tiles:
        raise SystemExit(
            "Refusing to bulk-download from tile.openstreetmap.org.\n"
            "Use a tile provider or self-hosted tile server that permits offline preloading, then pass its XYZ URL with --url-template.\n"
            "If you intentionally want the public OSM endpoint for a tiny test, add --allow-osm-public-tiles."
        )

    west, south, east, north = args.bbox
    args.out.mkdir(parents=True, exist_ok=True)

    total = 0
    for z in range(args.zoom_min, args.zoom_max + 1):
        x_min, y_max = latlon_to_tile(south, west, z)
        x_max, y_min = latlon_to_tile(north, east, z)
        for x in range(min(x_min, x_max), max(x_min, x_max) + 1):
            for y in range(min(y_min, y_max), max(y_min, y_max) + 1):
                total += download_tile(url_template, args.out, z, x, y, overwrite=args.overwrite)
                time.sleep(args.sleep)
    print(f"Downloaded or reused {total} tiles in {args.out}")


def latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    lat_rad = math.radians(lat)
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def download_tile(template: str, out_dir: Path, z: int, x: int, y: int, overwrite: bool) -> int:
    destination = out_dir / str(z) / str(x) / f"{y}.png"
    if destination.exists() and not overwrite:
        return 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = template.format(z=z, x=x, y=y)
    request = urllib.request.Request(url, headers={"User-Agent": "yp-ground-station/0.1 offline tile preloader"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read()
        if hashlib.sha1(data).hexdigest() in KNOWN_BLOCKED_TILE_SHA1:
            print(f"blocked {z}/{x}/{y}: provider returned a known access-blocked tile")
            return 0
        destination.write_bytes(data)
        print(f"saved {z}/{x}/{y}")
        return 1
    except urllib.error.HTTPError as exc:
        print(f"skip {z}/{x}/{y}: HTTP {exc.code}")
    except Exception as exc:
        print(f"skip {z}/{x}/{y}: {exc}")
    return 0


if __name__ == "__main__":
    main()
