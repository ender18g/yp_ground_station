# YP Ground Station

![YP Ground Station screenshot](screenshots/screen1.png)

Shipboard ground station for a Naval Academy Yard Patrol craft. The stack collects telemetry from USVs, UAVs, UUVs, a YP GPS feed, and an optional PX4/MAVROS UAV simulation, logs ROS-shaped messages to InfluxDB, and serves a local-first React/Leaflet map interface for monitoring and command.

## What Is Included

- `yp-server`: FastAPI service with native vehicle WebSockets, a lightweight rosbridge-compatible WebSocket, REST APIs, on-demand map tile caching, command routing, and InfluxDB logging.
- `web`: React + TypeScript + Leaflet UI with vehicle markers, headings, altitude labels, recent trails, YP range rings, hideable map layers, RTB commands, click-to-waypoint commands, and a live message drawer.
- `sim-vehicle`: Lightweight configurable simulated UAV, USV, or UUV container. Publishes heartbeat, `NavSatFix`, `Pose`, `BatteryState`, and `MultiDOFJointTrajectory` messages at 5 Hz.
- `yp-gps`: YP GPS publisher. Runs in simulated mode near the US Naval Academy or reads NMEA GPS data from a serial port.
- `arducopter_ws_bridge`: Hardware bridge that connects a real ArduPilot/MAVLink vehicle (Cube, Pixhawk, etc.) to the ground station over a WebSocket. Supports SAR mission dispatch.
- `px4-sitl-uav`: Optional profile-gated PX4 SITL multicopter simulation.
- `mavros`, `ros-master`, and `rosbridge`: Optional ROS/MAVROS path used by the PX4 UAV simulation.
- `px4-yp-bridge`: Optional bridge that discovers and subscribes to MAVROS topics through rosbridge, forwards MAVROS messages into the YP ground station, and translates YP waypoint/RTB commands back to MAVROS/PX4.
- `influxdb`: Time-series storage for telemetry and command messages.

## Quick Start

Start the normal lightweight stack:

```bash
docker compose up --build
```

Then open:

- Web UI: `http://localhost:8080`
- API docs: `http://localhost:8000/docs`
- API root/status links: `http://localhost:8000`
- InfluxDB: `http://localhost:8086`

The default compose file starts one existing simulated UAV, one simulated USV, one simulated UUV, and a simulated YP GPS source located near the Severn River off the US Naval Academy.

## PX4/MAVROS UAV Simulation

The PX4 UAV path is intentionally separate from the existing `sim-vehicle` containers. It only starts when the `px4` compose profile is enabled:

```bash
docker compose --profile px4 up --build
```

The PX4 profile starts:

- `px4-sitl-uav`: builds PX4 Autopilot `v1.15.4` inside `px4io/px4-dev-simulation-jammy`, bakes the `px4_sitl_default` binary into the image, and runs the Gazebo `gz_x500` multicopter SITL model with `PX4_SYS_AUTOSTART=4001`.
- `ros-master`: ROS Noetic master.
- `mavros`: connects to PX4 over MAVLink using `fcu_url:=udp://:14540@px4-sitl-uav:14580`.
- `rosbridge`: exposes the ROS graph over WebSocket at `ws://localhost:9090`.
- `px4-yp-bridge`: connects to rosbridge, subscribes to MAVROS telemetry topics, and connects to `yp-server` as vehicle `px4-uav`.

The first PX4 image build may take a while and needs internet access to clone PX4, its submodules, and Docker image layers. PX4 submodules are shallow-cloned for speed, with NuttX tags fetched explicitly because PX4's version-generation step reads those tags during the SITL build. The image also applies a small PX4 SITL compatibility patch that keeps the daemon socket alive when startup child processes interrupt `poll()`, and enables MAVLink broadcast on the offboard link so MAVROS can run in a separate Compose container. The SITL binary is built into the image so container startup does not rerun the full PX4 compile. The profile is not part of the normal quick start so day-to-day lightweight simulation still comes up quickly.

### MAVROS Topics Forwarded

`px4-yp-bridge` uses rosapi discovery through rosbridge to subscribe to every advertised `/mavros/...` topic it can see. It also keeps these core subscriptions configured up front so important topics are covered while the ROS graph is still coming up:

- `/mavros/state`
- `/mavros/extended_state`
- `/mavros/global_position/global`
- `/mavros/global_position/compass_hdg`
- `/mavros/global_position/rel_alt`
- `/mavros/local_position/pose`
- `/mavros/local_position/velocity_local`
- `/mavros/battery`
- `/mavros/imu/data`
- `/mavros/home_position/home`
- `/mavros/gpsstatus/gps1/raw`

The bridge also publishes canonical aliases that the existing map understands:

- `/vehicles/px4-uav/navsatfix`
- `/vehicles/px4-uav/pose`
- `/vehicles/px4-uav/battery`
- `/vehicles/px4-uav/heading`

Every forwarded topic is written into the ground station under `/vehicles/px4-uav/mavros/...`, ingested by `yp-server`, written to InfluxDB as `yp_messages`, and broadcast to the web UI message drawer.

## Scaling Existing Simulated Vehicles

To stress test with more of the lightweight simulated vehicles:

```bash
docker compose up --build --scale sim-uav=10 --scale sim-usv=4 --scale sim-uuv=3
```

Each simulator derives a unique ID from its container hostname unless `VEHICLE_ID` is explicitly set.

## Web UI

The map shows:

- Green USV markers
- Orange UAV markers
- Yellow UUV markers
- Gray YP marker
- Heading arrow for each vehicle
- Altitude beside each marker
- Adjustable recent trail duration
- Hover popup with telemetry
- Click modal with `RTB` and waypoint command actions
- Hideable map layer/source menu opened with the layer icon
- Optional YP range rings at 50 m, 100 m, and 200 m
- Live message drawer opened with the message icon
- SITL bridge panel (cable icon) to connect ArduPilot SITL instances by TCP/UDP address at runtime
- SAR mission patterns overlaid on the map when a grid search or MOB mission is dispatched; click the filled start dot to open a popup and clear the pattern manually

The Settings menu controls trail duration and YP range rings. The message drawer shows the newest live messages and the latest per-topic messages included in the initial vehicle snapshot, which helps inspect the extra MAVROS topics from `px4-uav`.

## ArduPilot SITL Bridge

The ground station includes a built-in MAVLink bridge that connects directly to ArduPilot or ArduCopter SITL instances at runtime — no separate bridge container required. This is useful for testing SAR missions against a simulated vehicle without deploying hardware.

### Connecting a SITL Instance

Open the **SITL** panel in the UI (cable icon in the top bar), enter a pymavlink-compatible connection string, and click **Connect**:

| Protocol | Example | Notes |
| --- | --- | --- |
| TCP client | `tcp:localhost:5760` | ArduPilot default SITL port |
| TCP server | `tcpin:0.0.0.0:5760` | Server waits for SITL to connect |
| UDP input | `udpin:0.0.0.0:14551` | Receive MAVLink datagrams |
| UDP output | `udpout:192.168.1.100:14550` | Send MAVLink datagrams to host |

Leave the **Vehicle ID** field empty to auto-derive an ID from the connection URL (e.g. `sitl-localhost-5760`), or enter a custom ID.

The bridge detects the vehicle frame type from the first MAVLink heartbeat and updates the map marker style accordingly. Telemetry is streamed at 10 Hz. Multiple SITL instances can be connected simultaneously.

### REST API

```http
GET  /api/sitl                      → list all active bridges
POST /api/sitl  { url, vehicle_id } → open a new bridge
DEL  /api/sitl/{vehicle_id}         → close and remove a bridge
```

### Supported Commands Over SITL Bridge

| Command type | Behaviour |
| --- | --- |
| `waypoint` | Sets the target lat/lon/alt |
| `rtb` | Commands RTL mode |
| `search_grid` | Generates and uploads a boustrophedon lawnmower mission, arms, and starts AUTO mode |
| `mob` | Generates and uploads a curved track-following MOB search mission, force-arms, and starts AUTO mode |

For `search_grid` and `mob`, the server holds the MAVLink connection exclusively during mission upload and arms the vehicle. The IO telemetry thread is paused while the mission is being uploaded to prevent ACK races.

## SAR Missions

The ground station can generate and dispatch Search and Rescue missions to any connected vehicle — SITL bridge or real hardware bridge.

### Search Grid

Right-click anywhere on the map, select **Search Grid**, choose a vehicle and the optional parameters, then click **Send**.

| Parameter | Default | Description |
| --- | --- | --- |
| Grid size | 200 m | Side length of the square search area |
| Swath width | 20 m | Track spacing (sensor coverage width) |
| Altitude | 30 m | Search altitude above home |

The server computes a boustrophedon (lawnmower) waypoint pattern centred on the clicked point, uploads the mission, arms the vehicle, and starts AUTO mode. The flight path is drawn on the map in the vehicle's colour as a dashed polyline.

### Man Overboard (MOB)

Click the **MOB** button in the top bar and confirm. The server:

1. Reads the YP vessel's recent position history to reconstruct the ship's track.
2. Generates a set of parallel lanes centred on the track and expanding outward — the number of lanes is determined by the corridor half-width divided by the swath width.
3. Dispatches the mission to the best available connected vehicle (preferring UAV SITL bridges, then any SITL bridge, then hardware bridges).

| Server variable | Default | Description |
| --- | --- | --- |
| `SAR_CORRIDOR_HALF_WIDTH_M` | `50.0` | Half the total search corridor around the YP track |
| `SAR_SWATH_M` | `20.0` | Lane spacing |
| `SAR_ALTITUDE_M` | `30.0` | Search altitude |
| `SAR_TAKEOFF_ALT_M` | `30.0` | Takeoff altitude before transitioning to search altitude |
| `SAR_CLIMB_SPEED_MS` | `8.0` | Climb speed in m/s |

If no SITL bridge or hardware bridge is connected, the MOB endpoint returns an error and the modal shows the reason inline — the YP GPS feed must also be running and have at least two position fixes.

### Pattern Overlay

When a SAR mission is dispatched the full flight path is broadcast to all connected UI clients and drawn on the map as a dashed polyline in the assigned vehicle colour. A filled dot marks the start waypoint and a hollow dot marks the end. The pattern persists until manually cleared: click the start dot and choose **Clear pattern** from the popup.

### SAR With Hardware Bridges

The `arducopter_ws_bridge` service supports the same `search_grid` and `mob` command types. When a command is routed to a hardware bridge vehicle, `arducopter_ws_bridge.py` receives it over its WebSocket, pauses telemetry, uploads the mission via direct MAVLink, arms, and starts AUTO mode. The pattern overlay is shown on the map at dispatch time.

## Waypoint And RTB Commands

To command a waypoint, click a vehicle, choose `Waypoint`, then click the map. The browser sends this message to `yp-server` over `/ws/ui`:

```json
{
  "op": "command",
  "vehicle_id": "px4-uav",
  "command": {
    "type": "waypoint",
    "target": {
      "latitude": 38.98495,
      "longitude": -76.47872,
      "altitude": 45.0
    }
  }
}
```

`latitude` and `longitude` are WGS84 decimal degrees from the clicked Leaflet map point. `altitude` is meters.

For the existing lightweight `sim-vehicle`, altitude is interpreted as a map/display altitude in meters and the simulator moves toward that latitude/longitude.

For the PX4/MAVROS vehicle, `px4-yp-bridge` translates the same command to a MAVROS publish on:

```text
/mavros/setpoint_raw/global
```

with message type:

```text
mavros_msgs/GlobalPositionTarget
```

The default `coordinate_frame` is `6`, MAVLink `MAV_FRAME_GLOBAL_RELATIVE_ALT_INT`. In that frame, latitude and longitude are WGS84 decimal degrees, and altitude is meters relative to PX4 home altitude. The bridge streams the setpoint at `SETPOINT_HZ` (default `5`) because PX4 Offboard mode requires a continuous setpoint stream. With `AUTO_ARM_OFFBOARD=true`, the bridge also calls:

```text
/mavros/cmd/arming true
/mavros/set_mode OFFBOARD
```

RTB sends:

```json
{ "type": "rtb" }
```

For the existing simulator this returns to its configured home point. For the PX4/MAVROS vehicle, the bridge calls:

```text
/mavros/set_mode AUTO.RTL
```

PX4 may reject arming, Offboard, or RTL if its simulated sensors, EKF state, preflight checks, or failsafe state are not ready. Check the `mavros`, `px4-sitl-uav`, and `px4-yp-bridge` logs when a command is acknowledged by the UI but not acted on by PX4.

## Message Transport

### Native Vehicle WebSocket

Vehicle clients connect to:

```text
ws://<server-host>:8000/ws/vehicle/<vehicle_id>
```

Publish messages shaped like:

```json
{
  "vehicle_id": "uav-alpha",
  "vehicle_type": "uav",
  "topic": "/vehicles/uav-alpha/navsatfix",
  "type": "sensor_msgs/msg/NavSatFix",
  "stamp": 1778952000.25,
  "msg": {
    "header": {
      "stamp": { "sec": 1778952000, "nanosec": 250000000 },
      "frame_id": "map"
    },
    "status": { "status": 0, "service": 1 },
    "latitude": 38.982,
    "longitude": -76.483,
    "altitude": 45.0,
    "position_covariance": [0, 0, 0, 0, 0, 0, 0, 0, 0],
    "position_covariance_type": 0
  }
}
```

Commands sent back to vehicles over the same socket look like:

```json
{
  "op": "command",
  "vehicle_id": "uav-alpha",
  "command": {
    "type": "waypoint",
    "target": { "latitude": 38.9825, "longitude": -76.4841, "altitude": 35.0 }
  }
}
```

### YP Rosbridge-Like WebSocket

The server also exposes a small rosbridge-like endpoint:

```text
ws://<server-host>:8000/ws/rosbridge
```

Supported operations:

- `publish`
- `subscribe`
- `unsubscribe`
- `command`

Example publish:

```json
{
  "op": "publish",
  "topic": "/vehicles/uav-alpha/navsatfix",
  "type": "sensor_msgs/msg/NavSatFix",
  "msg": {
    "latitude": 38.982,
    "longitude": -76.483,
    "altitude": 45.0
  }
}
```

This endpoint is intentionally small and practical. The PX4/MAVROS profile uses a real `rosbridge_server` container for the ROS graph, then `px4-yp-bridge` converts those messages into the native YP vehicle WebSocket.

## ROS-Shaped Messages

The simulator, server, and bridge use JSON messages matching the main ROS field names for:

- `trajectory_msgs/msg/MultiDOFJointTrajectoryPoint`
- `trajectory_msgs/msg/MultiDOFJointTrajectory`
- `sensor_msgs/msg/NavSatFix`
- `geometry_msgs/msg/Pose`
- `geometry_msgs/msg/PoseStamped`
- `sensor_msgs/msg/BatteryState`
- `sensor_msgs/msg/Imu`
- `mavros_msgs/msg/State`
- `mavros_msgs/msg/GlobalPositionTarget`

The server does not require ROS to be installed for the normal stack. ROS is only required for the optional PX4/MAVROS profile.

## YP GPS

The default compose file runs:

```yaml
GPS_MODE: sim
HOME_LAT: "38.984764"
HOME_LON: "-76.478643"
HEADING_DEG: "330"
SPEED_KNOTS: "3"
```

To use a real serial GPS, change the `yp-gps` service:

```yaml
environment:
  GPS_MODE: serial
  SERIAL_PORT: /dev/ttyUSB0
  BAUD_RATE: 9600
devices:
  - /dev/ttyUSB0:/dev/ttyUSB0
```

The GPS container publishes the YP as a `yp` vehicle with `NavSatFix`, `Pose`, `BatteryState`, and heartbeat messages so the map can center on the ship.

In simulated mode, the YP starts at latitude `38.984764`, longitude `-76.478643`, heading `330` degrees, and moves at `3` knots unless those environment variables are changed.

## Map Tiles

The web UI has two map bases:

- `Street Maps`: OpenStreetMap raster tiles.
- `Satellite`: Esri World Imagery tiles.

Each map base has three source modes:

- `Auto`: serve from cache when present; otherwise fetch the visible tile from the configured online source and cache it.
- `Cached only`: serve only previously cached tiles.
- `Online only`: request tiles directly from the public provider in the browser.

The server tile proxy uses:

```text
http://localhost:8000/tiles/osm/<z>/<x>/<y>.png
http://localhost:8000/tiles/earth/<z>/<x>/<y>.png
```

When the operator views or pans the map, Leaflet requests only the tiles needed for the current viewport. The server fetches missing tiles from:

```text
https://tile.openstreetmap.org/{z}/{x}/{y}.png
https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}
```

Fetched tiles are cached on the server in:

```text
data/tile-cache/
```

That folder is mounted into Docker as `/data/tile-cache`, so cached map tiles persist across container restarts and rebuilds. If a tile is not cached and the server cannot reach the internet, the server returns a plain light-blue tile instead of a broken image.

Check cache status:

```bash
curl http://localhost:8000/api/tile-cache
```

Clear the cache:

```bash
rm -rf data/tile-cache
```

The app does not bulk download, pre-seed, or scan map areas. It caches only tiles requested by the active map viewport. OpenStreetMap's public tile service allows normal interactive viewing and local caching according to HTTP cache headers, but prohibits bulk downloads and offline prefetch features. If this system later needs guaranteed offline maps for large areas, use a self-hosted tile server or a provider that explicitly allows offline packages.

## Configuration

Useful environment variables:

| Service | Variable | Default | Description |
| --- | --- | --- | --- |
| `yp-server` | `INFLUX_URL` | `http://influxdb:8086` | InfluxDB URL |
| `yp-server` | `INFLUX_ORG` | `yp` | InfluxDB org |
| `yp-server` | `INFLUX_BUCKET` | `telemetry` | InfluxDB bucket |
| `yp-server` | `INFLUX_TOKEN` | `yp-dev-token` | InfluxDB token |
| `yp-server` | `TILE_CACHE_DIR` | `/data/tile-cache` | Persistent on-demand map tile cache |
| `yp-server` | `OSM_TILE_URL` | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` | Street tile source URL template |
| `yp-server` | `EARTH_TILE_URL` | `https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}` | Satellite tile source URL template |
| `yp-server` | `VEHICLE_TTL_SECONDS` | `30` | Seconds before an unheard vehicle is considered stale |
| `yp-server` | `HISTORY_MAX_POINTS` | `5000` | Maximum position history points kept per vehicle |
| `yp-server` | `SAR_CORRIDOR_HALF_WIDTH_M` | `50.0` | MOB search corridor half-width in metres |
| `yp-server` | `SAR_SWATH_M` | `20.0` | SAR lane spacing in metres |
| `yp-server` | `SAR_ALTITUDE_M` | `30.0` | SAR search altitude in metres |
| `yp-server` | `SAR_TAKEOFF_ALT_M` | `30.0` | SAR takeoff altitude in metres |
| `yp-server` | `SAR_CLIMB_SPEED_MS` | `8.0` | SAR climb speed in m/s |
| `sim-*` | `VEHICLE_TYPE` | `uav` | `uav`, `usv`, or `uuv` |
| `sim-*` | `VEHICLE_ID` | auto | Optional fixed vehicle ID |
| `sim-*` | `HOME_LAT` | `38.9822` | RTB/home latitude |
| `sim-*` | `HOME_LON` | `-76.4819` | RTB/home longitude |
| `yp-gps` | `GPS_MODE` | `sim` | `sim` or `serial` |
| `yp-gps` | `SERIAL_PORT` | `/dev/ttyUSB0` | NMEA GPS serial device |
| `yp-gps` | `HEADING_DEG` | `330` | Simulated YP heading in degrees |
| `yp-gps` | `SPEED_KNOTS` | `3` | Simulated YP speed |
| `px4-sitl-uav` | `PX4_HOME_LAT` | `38.98490` | PX4 SITL home latitude |
| `px4-sitl-uav` | `PX4_HOME_LON` | `-76.47880` | PX4 SITL home longitude |
| `px4-sitl-uav` | `PX4_HOME_ALT` | `45.0` | PX4 SITL home altitude |
| `px4-sitl-uav` | `PX4_SYS_AUTOSTART` | `4001` | PX4 x500 airframe autostart ID |
| `px4-sitl-uav` | `PX4_SIM_MODEL` | `gz_x500` | PX4 Gazebo vehicle model |
| `px4-sitl-uav` | `PX4_GZ_MODEL` | `gz_x500` | Gazebo model spawned by PX4 |
| `px4-yp-bridge` | `VEHICLE_ID` | `px4-uav` | Vehicle ID shown in the YP UI |
| `px4-yp-bridge` | `ROSBRIDGE_URL` | `ws://rosbridge:9090` | Real rosbridge WebSocket URL |
| `px4-yp-bridge` | `SETPOINT_HZ` | `5` | Global setpoint publish rate |
| `px4-yp-bridge` | `AUTO_ARM_OFFBOARD` | `true` | Arm and switch to Offboard after waypoint command |
| `px4-yp-bridge` | `GLOBAL_SETPOINT_FRAME` | `6` | MAVROS `GlobalPositionTarget.coordinate_frame` |
| `px4-yp-bridge` | `DISCOVER_MAVROS_TOPICS` | `true` | Subscribe to every rosapi-discovered `/mavros/...` topic |

## Development

Run just the backend locally:

```bash
cd services/server
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Run the frontend locally:

```bash
cd web
npm install
npm run dev
```

## Notes For Real Vehicles

- Keep vehicle IDs stable and unique.
- Prefer one WebSocket per vehicle.
- Use the native WebSocket first for the smallest moving part count.
- Add authentication before putting this on anything other than a trusted local shipboard network.
- Validate waypoint commands on the vehicle side before forwarding to the Cube/Pixhawk.
- Keep a manual RC/safety pilot path independent of the web UI.
