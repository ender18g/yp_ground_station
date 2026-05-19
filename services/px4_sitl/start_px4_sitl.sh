#!/usr/bin/env bash
set -euo pipefail

cd /PX4-Autopilot

python3 - <<'PY'
import os
import pathlib
import re

world_path = pathlib.Path("/PX4-Autopilot/Tools/simulation/gz/worlds/default.sdf")
text = world_path.read_text()

for tag, env_name in (
    ("latitude_deg", "PX4_HOME_LAT"),
    ("longitude_deg", "PX4_HOME_LON"),
):
    value = os.getenv(env_name)
    if value:
        text = re.sub(
            rf"<{tag}>[^<]*</{tag}>",
            f"<{tag}>{value}</{tag}>",
            text,
            count=1,
        )

world_path.write_text(text)
PY

exec ./build/px4_sitl_default/bin/px4 \
    -d \
    -i 0 \
    -w /PX4-Autopilot/build/px4_sitl_default/rootfs \
    /PX4-Autopilot/build/px4_sitl_default/etc
