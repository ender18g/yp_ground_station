# YP Ground Station

Shipboard ground station for a Naval Academy Yard Patrol craft. The stack collects vehicle telemetry from USVs, UAVs, UUVs, and the YP GPS feed, logs ROS2-shaped messages to InfluxDB, and serves a local-first React map interface for monitoring and command.

## What Is Included

- `yp-server`: FastAPI service with native vehicle WebSockets, a lightweight rosbridge-compatible WebSocket, REST APIs, offline tile serving, command routing, and InfluxDB logging.
- `web`: React + TypeScript + Leaflet UI with a full-screen map, vehicle markers, altitude labels, headings, recent trails, hover data, RTB commands, and click-to-waypoint commands.
- `sim-vehicle`: Configurable simulated UAV, USV, or UUV container that publishes heartbeat, `NavSatFix`, `Pose`, `BatteryState`, and `MultiDOFJointTrajectory` messages at 5 Hz and accepts commands.
- `yp-gps`: YP GPS publisher. It can run in simulated mode near the US Naval Academy or read NMEA GPS data from a serial port.
- `influxdb`: Time-series storage for all telemetry and command messages.
- `tileserver`: Optional local TileServer GL service for offline `.mbtiles` maps.
- `scripts/download_tiles.py`: Offline OpenStreetMap tile preloader.

## Quick Start

```bash
docker compose up --build
```

Then open:

- Web UI: `http://localhost:8080`
- API docs: `http://localhost:8000/docs`
- API root/status links: `http://localhost:8000`
- InfluxDB: `http://localhost:8086`

The default compose file starts one simulated UAV, one simulated USV, one simulated UUV, and a simulated YP GPS source located near the Severn River off the US Naval Academy.

## Scaling Simulated Vehicles

To stress test with more simulated vehicles:

```bash
docker compose up --build --scale sim-uav=10 --scale sim-usv=4 --scale sim-uuv=3
```

Each simulator derives a unique ID from its container hostname unless `VEHICLE_ID` is explicitly set.

## Web UI

The map shows:

- Green USV markers
- Orange UAV markers
- Yellow UUV markers
- Blue YP marker
- Heading arrow for each vehicle
- Altitude beside each marker
- Adjustable recent trail duration
- Hover popup with telemetry
- Click modal with `RTB` and waypoint command actions

To command a waypoint, click a vehicle, choose `Waypoint`, then click the map. The server sends a command message to that vehicle's WebSocket connection.

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
    "position_covariance": [0,0,0,0,0,0,0,0,0],
    "position_covariance_type": 0
  }
}
```

Commands sent back to vehicles look like:

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

RTB commands use:

```json
{ "type": "rtb" }
```

### Rosbridge-Like WebSocket

ROS-style clients can connect to:

```text
ws://<server-host>:8000/ws/rosbridge
```

Supported operations:

- `publish`
- `subscribe`
- `unsubscribe`

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

This is intentionally small and practical. It is not a full rosbridge replacement yet, but it keeps topic names and message fields compatible with ROS2 conventions.

## ROS2-Shaped Messages

The simulator and server use JSON messages matching the main ROS2 field names for:

- `trajectory_msgs/msg/MultiDOFJointTrajectoryPoint`
- `trajectory_msgs/msg/MultiDOFJointTrajectory`
- `sensor_msgs/msg/NavSatFix`
- `geometry_msgs/msg/Pose`
- `sensor_msgs/msg/BatteryState`

The trajectory and pose messages can be telemetry from a vehicle or command-like messages sent toward a vehicle.

## Raspberry Pi Vehicle Container Path

For real vehicle Raspberry Pis, start from `services/sim_vehicle` and replace the simulator movement loop with MAVLink IO:

1. Read Cube/Pixhawk telemetry using MAVLink, MAVROS, or a ROS2 bridge.
2. Convert telemetry into the ROS2-shaped JSON messages above.
3. Publish to `/ws/vehicle/<vehicle_id>` at the desired rate.
4. Listen on the same WebSocket for `waypoint`, `trajectory`, and `rtb` commands.
5. Convert commands back to MAVLink mission/setpoint/RTL commands.

The server does not require ROS to be installed, but the topic names and message payloads are intentionally ROS-friendly.

## YP GPS

The default compose file runs:

```yaml
GPS_MODE: sim
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

## Offline Maps

Tiles are served from:

```text
data/tiles/<z>/<x>/<y>.png
```

The compose file mounts that directory into both `yp-server` and `web` through the server API. The frontend first tries local tiles at `/tiles/{z}/{x}/{y}.png`. If internet access exists, the UI also has an online OpenStreetMap layer.

### The Local Tile Server URL

If you are running everything locally, `your-tile-server.example` should be a tile server running on your own machine or on the YP server. The compose file includes an optional TileServer GL service for this.

Put an MBTiles file here:

```text
data/maps/usna.mbtiles
```

Then start the local tile server:

```bash
docker compose --profile maps up -d tileserver
```

Open `http://localhost:8081` to confirm the map loads. TileServer GL exposes rendered raster tiles at:

```text
http://localhost:8081/styles/<style-id>/{z}/{x}/{y}.png
```

The style id is listed at `http://localhost:8081/styles.json`. For many OpenMapTiles-compatible files the preview style is `basic-preview`, so the downloader URL is often:

```bash
--url-template "http://localhost:8081/styles/basic-preview/{z}/{x}/{y}.png"
```

If `basic-preview` is not listed in `styles.json`, replace it with the actual style id from your server.

### Preload US Naval Academy Tiles

Run this before deploying to the YP using a tile provider, internal tile server, or purchased/offline tile source that permits bulk download and offline caching:

```bash
python3 scripts/download_tiles.py \
  --bbox -76.505 38.965 -76.455 39.005 \
  --zoom-min 12 \
  --zoom-max 17 \
  --out data/tiles \
  --url-template "http://localhost:8081/styles/basic-preview/{z}/{x}/{y}.png"
```

The bbox format is:

```text
west south east north
```

For additional operating areas, run the script again with a different bounding box. The script keeps the standard `{z}/{x}/{y}.png` layout, so new areas can be mixed in the same `data/tiles` directory.

Do not use the public `tile.openstreetmap.org` endpoint for offline preloading. It is intended for interactive map browsing, not bulk caching, and it can return blocked placeholder tiles. If blocked placeholder images were already cached, remove `data/tiles` and rerun the downloader with an approved tile source.

You still need a real offline map file. Good sources are:

- A purchased/offline `.mbtiles` package from a map provider.
- An internally generated OpenMapTiles-compatible `.mbtiles` file from OSM data.
- A locally hosted raster tile service approved for caching.

## Configuration

Useful environment variables:

| Service | Variable | Default | Description |
| --- | --- | --- | --- |
| `yp-server` | `INFLUX_URL` | `http://influxdb:8086` | InfluxDB URL |
| `yp-server` | `INFLUX_ORG` | `yp` | InfluxDB org |
| `yp-server` | `INFLUX_BUCKET` | `telemetry` | InfluxDB bucket |
| `yp-server` | `INFLUX_TOKEN` | `yp-dev-token` | InfluxDB token |
| `yp-server` | `TILE_DIR` | `/data/tiles` | Offline tile directory |
| `sim-*` | `VEHICLE_TYPE` | `uav` | `uav`, `usv`, or `uuv` |
| `sim-*` | `VEHICLE_ID` | auto | Optional fixed vehicle ID |
| `sim-*` | `HOME_LAT` | `38.9822` | RTB/home latitude |
| `sim-*` | `HOME_LON` | `-76.4819` | RTB/home longitude |
| `yp-gps` | `GPS_MODE` | `sim` | `sim` or `serial` |
| `yp-gps` | `SERIAL_PORT` | `/dev/ttyUSB0` | NMEA GPS serial device |

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
