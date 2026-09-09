#!/usr/bin/env python3
"""Build a self-contained Pi bridge directory without duplicating shared source.

Example: python companion_vehicle_software/bundle_bridge.py arducopter /tmp/arducopter
Copy the resulting directory to the Pi, install requirements.txt, and run the
existing bridge entrypoint there. The destination must not already exist.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil

BRIDGES = {
    "arducopter": "arducopter_piScripts",
    "blueboat": "blueboat_piScripts",
}


def bundle_bridge(bridge: str, destination: Path) -> Path:
    """Copy an entrypoint directory and the shared package into a fresh folder."""
    companion_dir = Path(__file__).resolve().parent
    source = companion_dir / BRIDGES[bridge]
    destination = destination.resolve()
    if destination.is_relative_to(source):
        raise ValueError("The bundle destination must be outside the source directory")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", "venv")
    shutil.copytree(source, destination, ignore=ignore)
    shutil.copytree(companion_dir.parent / "yp_common", destination / "yp_common", ignore=ignore)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bridge", choices=BRIDGES)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(bundle_bridge(args.bridge, args.destination))


if __name__ == "__main__":
    main()
