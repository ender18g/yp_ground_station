# TRIDENT YP

**Telemetry, Remote Intelligence, Data, Electronic Navigation, and Tasking - Yard Patrol**

Shipboard ground station for a Naval Academy Yard Patrol craft. The stack collects telemetry from USVs, UAVs, UUVs, a YP GPS feed, and an optional PX4/MAVROS UAV simulation; logs ROS-shaped messages to InfluxDB; and serves a local-first React/Leaflet map interface for monitoring and command.

## Contents

- [Overview](#overview)
- [Quick start](#quick-start)
- [User interface](#user-interface)
- [Accounts and permissions](#accounts-and-permissions)
- [Vehicle connections](#vehicle-connections)
- [Commanding and mission planning](#commanding-and-mission-planning)
- [Search and rescue operations](#search-and-rescue-operations)
- [Video streams](#video-streams)
- [Vehicle deconfliction](#vehicle-deconfliction)
- [Advanced integrations](#advanced-integrations)
- [Message transport](#message-transport)
- [Maps, GPS, and configuration](#maps-gps-and-configuration)
- [Development](#development)
- [Notes for real vehicles](#notes-for-real-vehicles)

## Overview

### Included services

- `yp-server`: FastAPI service with native vehicle WebSockets, a lightweight rosbridge-compatible WebSocket, REST APIs, on-demand map tile caching, command routing, automatic vehicle deconfliction, InfluxDB logging, and SQLite/JWT account authorization.
- `web`: React, TypeScript, Leaflet, and Three.js UI with vehicle markers, headings, altitude labels, trails, YP range rings, map layers, commands, mission planning, live messages, login, user management, settings, and video playback.
- `sim-vehicle`: Configurable simulated UAV, USV, UUV, or UGV. Publishes heartbeat, `NavSatFix`, `Pose`, `BatteryState`, and `MultiDOFJointTrajectory` messages at 5 Hz; supports SAR missions and temporary deconfliction detours.
- `sim-umaa`: Loopback UMAA vehicle for testing the ground-station workflow before real DDS topics are available.
- `yp-gps`: Simulated or serial NMEA YP GPS publisher.
- `arducopter_ws_bridge`: Hardware WebSocket bridge for real ArduPilot/MAVLink vehicles.
- `px4-sitl-uav`, `mavros`, `ros-master`, `rosbridge`, and `px4-yp-bridge`: Optional PX4/MAVROS simulation path.
- `umaa-bridge`: RTI Connext DDS bridge shell for a real UMAA vehicle.
- `influxdb`: Time-series storage for telemetry and command messages.
- `companion_vehicle_software`: Vehicle-side BlueBoat, ArduPilot, and YP emulator scripts.

### Repository map

| Path | Purpose |
| --- | --- |
| `web/` | React frontend and production Nginx image |
| `services/server/` | FastAPI backend and authorization/database code |
| `services/sim_vehicle/` | Lightweight simulated vehicles |
| `services/umaa_bridge/` | UMAA loopback and RTI adapter |
| `services/arducopter_ws_bridge/` | ArduPilot WebSocket bridge |
| `services/px4_*` and `services/mavros/` | Optional PX4/MAVROS path |
| `services/yp_gps/` | YP GPS publisher |
| `services/telemetry_radio_bridge.py` | Standalone serial-radio bridge |
| `services/com_tcp_relay.py` | Windows COM-to-TCP relay |
| `companion_vehicle_software/` | Companion-computer integrations |
| `scripts/download_tiles.py` | Optional offline tile-source helper |
| `data/auth/` and `data/tile-cache/` | Local persistent runtime data |

## Quick start

Start the normal lightweight stack:

```bash
docker compose up --build
```

Open:

- Web UI: `http://localhost:8080`
- API docs: `http://localhost:8000/docs`
- API root/status links: `http://localhost:8000`
- InfluxDB: `http://localhost:8086`

The default compose file starts two simulated UAVs (`sim-uav1`, `sim-uav2`), one simulated USV, one simulated UUV, the `sim-umaa` loopback vehicle, and a simulated YP GPS source near the Severn River off the US Naval Academy.

The normal stack does not require ROS. ROS is only required for the optional PX4/MAVROS profile.

## User interface

![Global Map](screenshots/global.png)

The top bar provides these navigation modes and tools:

| Control | Purpose |
| --- | --- |
| Global Map | Live operational map with vehicles, trails, commands, weather, and overlays |
| Mission Planner | Create, edit, import, export, and upload full waypoint missions |
| Local Waypoint Planner | Build ship-relative waypoint trajectories using the current YP position |
| Vehicle Connections | Connect network MAVLink endpoints or RFD-900 serial radios |
| Settings | Display, vessel, deconfliction, and MOB configuration |
| Messages | Live message drawer with per-topic filtering and retained snapshots |
| User Management | Available to accounts with `manage_users` |
| Logout | End the current session |

The map displays icons for USV, UAV (quad and fixed wing), UUV, UGV,and YP; heading; altitude; telemetry popups; recent trails; optional YP range rings; SAR patterns; an optional NOAA radar layer; Open-Meteo wind vectors; and a bottom-center wind readout. The layer button selects Street Maps or Satellite and `Auto`, `Cached only`, or `Online only` tile behavior.

### Vehicle modal

<p align="center">
<img src="screenshots/vehicle_modal.png" alt="Vehicle modal" height="300">
</p>

Click a vehicle marker to open its draggable modal. It provides real-time position, altitude, heading, battery, SAR status, and, when a mother ship is selected, forward/left/up and radial ship-frame distances. Depending on permissions and vehicle type it also provides RTB, Waypoint, flight-mode controls, video, and a marker color picker.

### Settings

<p align="center">
<img src="screenshots/settings.png" alt="Settings" height="300">
</p>

The settings tabs appear in the UI as Display, Deconfliction, Man Overboard, and Vessel.

- **Display:** trail window, YP range rings, and database message retention.
- **Vessel:** choose a connected vehicle as the YP mother vessel, or use the dedicated `yp-gps` service; configure RTB update rate and stern distance.
- **Deconfliction:** enable the feature, configure global and per-type safety radii, avoidance orbit radius, and maximum pause duration.
- **Man Overboard:** configure track length, swath width, search altitude, corridor width, takeoff altitude, and climb speed.

Settings persist in SQLite and are available through `GET` and `PUT /api/settings`. Deconfliction settings use `GET` and `PUT /api/deconfliction/settings`.

### Flight log export

Users with the `manage_settings` permission can open the disk icon in the top toolbar, choose a duration, and download retained `yp_messages` data. Exports are gzip-compressed JSON Lines (`.jsonl.gz`) files that can be opened with standard tools on Windows, macOS, and Linux. The first decompressed line contains metadata, followed by records with `timestamp`, `vehicle_id`, `vehicle_type`, and `fields`; heartbeat messages are excluded.

On Linux or macOS, this is a gzip-compressed text file rather than a tar archive. Use `gzip -t flight-log.jsonl.gz` to validate it, then `gzip -dk flight-log.jsonl.gz` to create `flight-log.jsonl` while keeping the compressed file. Do not use `tar -xzf`, which expects a `.tar.gz` archive and can report `missing type keyword in mtree specification` for this file. On Windows, 7-Zip can extract the `.gz` file directly.

Exports do not modify InfluxDB and include only data still retained there. The `message_retention_seconds` setting may remove older records before they can be exported.

### Demo and view-only modes

Static demo mode renders local vehicles without a live server. Use `/demo`, `?demo=true`, or build with `VITE_STATIC_DEMO=true`.

View-only mode uses `/view` or `?view=true`:

```text
http://localhost:8080/view
http://localhost:8080/?view=true
```

Live telemetry remains available, but commands are blocked for real hardware vehicles, the Connections panel is hidden, and a **View only** badge is shown. Vehicles whose IDs start with `sim-` remain commandable.

## Accounts and permissions

<p align="center">
<img src="screenshots/users.png" alt="User Management" height="300">
</p>

The web UI requires a username and password. On first server startup, the default development account is created:

```text
Username: admin
Password: admin
```

Change it immediately. Never use these credentials on a reachable or operational network. Set a unique `JWT_SECRET` for non-development deployments.

Administrators open **User Management** from the users icon. The panel creates and deletes accounts, resets passwords, applies role presets, and saves custom permissions. The server enforces authorization on protected REST endpoints and UI WebSocket commands; hiding a button is not authorization.

| Role | Capabilities |
| --- | --- |
| `view_only` | Read telemetry and vehicle status |
| `waypoint_command` | View permissions plus waypoints, RTB, mode changes, and SAR cancellation |
| `mission_planning` | Waypoint permissions plus mission creation, upload, and search grids |
| `man_overboard` | Mission planning plus MOB dispatch |
| `admin` | All operational permissions plus settings, connections, video streams, and user management |

Custom permissions: `read_telemetry`, `read_vehicle_status`, `send_waypoint`, `send_rtb`, `set_vehicle_mode`, `cancel_sar`, `create_mission`, `upload_mission`, `search_grid`, `trigger_mob`, `manage_sitl`, `manage_settings`, `manage_video_streams`, and `manage_users`.

Accounts are stored in SQLite at `data/auth/auth.db`, mounted into `yp-server` as `/data/auth/auth.db`. They persist across rebuilds and container recreation. Deleting the database intentionally resets the store to `admin` / `admin`; the database contains password hashes and is excluded from Git.

## Vehicle connections

<p align="center">
<img src="screenshots/vehicle_connections.png" alt="Vehicle Connections" height="300">
</p>

### Network MAVLink and ArduPilot SITL

Open **Vehicle Connections** and use the **Network** tab. Supported connection strings include:

| Protocol | Example | Notes |
| --- | --- | --- |
| TCP client | `tcp:localhost:5760` | ArduPilot default SITL port |
| TCP server | `tcpin:0.0.0.0:5760` | Waits for SITL to connect |
| UDP input | `udpin:0.0.0.0:14551` | Receives MAVLink datagrams |
| UDP output | `udpout:192.168.1.100:14550` | Sends MAVLink datagrams |
| Serial | `serial:/dev/ttyUSB0:57600` | Native Linux/device passthrough |

Vehicle ID and Camera Host are optional. IDs are derived from the URL when omitted. The bridge detects vehicle frame type from the first heartbeat, streams telemetry at 10 Hz, and supports multiple simultaneous connections.

REST API:

```http
GET    /api/sitl
POST   /api/sitl                    { url, vehicle_id, camera_host }
DELETE /api/sitl/{vehicle_id}
```

### RFD-900 and telemetry radios

On Windows, Docker Desktop cannot directly access COM ports. Run this on the Windows host:

```bash
pip install pyserial
python services/com_tcp_relay.py --port COM12 --baud 57600 --tcp-port 5762
```

Then use the RFD-900 tab, or enter `tcp:host.docker.internal:5762` in the Network tab. The relay keeps the COM port open and accepts a new Docker connection after disconnects. On Linux/native Docker, uncomment the `devices` mapping under `yp-server` and use `serial:/dev/ttyUSB0:57600`.

```http
GET /api/serial-ports
```

The standalone `services/telemetry_radio_bridge.py` can also forward a serial radio directly to the YP WebSocket.

### Hardware and companion bridges

- `arducopter_ws_bridge` connects a real ArduPilot/MAVLink vehicle over WebSocket and supports waypoint, RTB, SAR, and flight-mode commands.
- `companion_vehicle_software/arducopter_piScripts/` contains Raspberry Pi ArduPilot bridge variants and configuration.
- `companion_vehicle_software/blueboat_piScripts/` contains BlueBoat bridge variants.
- `companion_vehicle_software/Hunter_YPEmulator/` contains the YP emulator.

The other standalone bridge utilities are:

- `services/server/app/main.py`: FastAPI SITL bridge with waypoint, RTB, SAR, mission-upload, and flight-mode support.
- `services/px4_mavros_bridge/px4_mavros_bridge.py`: ROS/MAVROS to YP bridge with PX4 mode mapping.
- `services/umaa_bridge/umaa_bridge.py`: RTI Connext DDS bridge for UMAA vehicles.
- `companion_vehicle_software/blueboat_piScripts/simplified_bridge.py`: Minimal MAVLink-to-YP telemetry bridge example.

## Commanding and mission planning

### Waypoint and RTB commands

Click a vehicle, choose **Waypoint**, and click the map. The browser sends a command over `/ws/ui`:

```json
{
  "op": "command",
  "vehicle_id": "px4-uav",
  "command": {
    "type": "waypoint",
    "target": { "latitude": 38.98495, "longitude": -76.47872, "altitude": 45.0 }
  }
}
```

`latitude` and `longitude` are WGS84 decimal degrees; altitude is metres. RTB sends `{ "type": "rtb" }` and starts persistent stern-follow mode. The server targets the configured distance aft of the YP heading and updates it at `rtb_update_hz` until canceled or retasked. Vehicles approaching from the bow or beam are routed around an aft quarter rather than across the YP safety envelope.

For PX4, waypoint commands are translated to `/mavros/setpoint_raw/global` using `mavros_msgs/GlobalPositionTarget`, frame `6` (`MAV_FRAME_GLOBAL_RELATIVE_ALT_INT`), and a default stream rate of 5 Hz. `AUTO_ARM_OFFBOARD=true` also requests `/mavros/cmd/arming true` and `/mavros/set_mode OFFBOARD`. PX4 may reject commands when sensors, EKF, preflight, or failsafe state are not ready; inspect the PX4, MAVROS, and bridge logs.

All bridge types support these command types where the vehicle can execute them:

| Command | Behavior |
| --- | --- |
| `waypoint` | Set a target latitude, longitude, and altitude |
| `rtb` | Start continuous server-side stern-follow |
| `search_grid` | Stream a boustrophedon SAR mission |
| `mob` | Stream a curved track-following MOB mission |
| `cancel_sar` | Cancel an active streaming SAR mission |
| `mission_plan` | Upload a waypoint sequence and optionally arm/start AUTO |
| `set_mode` | Change the vehicle flight mode |

Available mode lists are vehicle-specific. ArduPilot UAVs support `STABILIZE`, `ACRO`, `ALT_HOLD`, `AUTO`, `GUIDED`, `LOITER`, `RTL`, `CIRCLE`, `LAND`, `DRIFT`, `SPORT`, `FLIP`, `AUTOTUNE`, and `POSHOLD`; ArduPilot USV/UGV support `MANUAL`, `GUIDED`, `AUTO`, `RTL`, `LOITER`, and `CIRCLE`; PX4 UAVF supports `MANUAL`, `ALTITUDE_CONTROL`, `POSITION_CONTROL`, `AUTO`, `OFFBOARD`, and `EMERGENCY`.

### Mission Planner

![Mission Planner](screenshots/mission_planner.png)

The **Mission Planner** tab is the full-mission editor. It supports:

- map-click waypoint creation, dragging, editing, reordering, and deletion;
- target vehicle selection and default altitude;
- waypoint, takeoff, loiter-time, land, RTL, and `DO_JUMP` items;
- per-waypoint altitude, hold time, acceptance radius, parameter 3, and yaw;
- optional GUIDED mode after completion;
- native JSON, QGroundControl `.plan`, and Mission Planner `.waypoints`/WPL import and export;
- one-click upload that arms, starts, and sends the mission in AUTO mode.

Published mission overlays remain on the map until cleared.

### Local Waypoint Planner

![Local Waypoint Planner](screenshots/local_waypoint_planner.png)

The **Local Waypoint Planner** tab is separate from Mission Planner. It creates ship-relative trajectories from the current YP position and provides spatial context for local operations. It uses the `ship_relative_trajectory` command with `RelativeWaypoint` values (`x`, `y`, `z`).

## Search and rescue operations

![SAR overlay](screenshots/sar_overlay.png)

### Search Grid

Right-click the map, select **Search Grid**, choose a vehicle, and send. The server creates a centered boustrophedon/lawnmower pattern, dispatches it, and draws the assigned vehicle's path.

| Parameter | Default | Description |
| --- | --- | --- |
| Grid size | 200 m | Side length of the square search area |
| Swath width | 20 m | Track spacing/sensor coverage width |
| Altitude | 30 m | Search altitude above home |

### Man Overboard (MOB)

Click **MOB**, choose an optional dispatch vehicle, and confirm. UGVs and the YP are excluded. The server reads recent YP fixes, builds parallel lanes around the track, and prefers UAV SITL, then other SITL, then hardware bridges. The YP GPS feed must be running with at least two fixes.

| Variable | Default | Description |
| --- | --- | --- |
| `SAR_CORRIDOR_HALF_WIDTH_M` | `50.0` | Search corridor half-width |
| `SAR_SWATH_M` | `20.0` | Lane spacing |
| `SAR_ALTITUDE_M` | `30.0` | Search altitude |
| `SAR_TAKEOFF_ALT_M` | `30.0` | Takeoff altitude |
| `SAR_CLIMB_SPEED_MS` | `8.0` | Climb speed in m/s |

Both SAR modes support streaming carrot-chase execution through SITL and hardware ArduPilot bridges. The full path is broadcast to UI clients; a filled start dot and hollow end dot identify the pattern. Click the start dot to clear it. `cancel_sar` stops an active SAR mission.

## Video streams

The vehicle modal shows **Stream Video** when `video.enabled=true` and either `video.streams` or `video.playback_url` is available. Each stream can be supplied as:

```json
{ "label": "Bow Camera", "url": "http://<whep-host>/<stream-id>/whep" }
```

The frontend negotiates WebRTC using WHEP by POSTing SDP offers to the selected URL. The backend metadata API is:

```http
GET    /api/video/streams
PUT    /api/video/streams/{vehicle_id}
DELETE /api/video/streams/{vehicle_id}
GET    /api/video/streams?include_sources=true
```

Example:

```bash
curl -X PUT http://localhost:8000/api/video/streams/blueboat-03 \
  -H 'Content-Type: application/json' \
  -d '{"source_rtsp_url":"rtsp://user:pass@10.0.0.23:554/stream1","stream_id":"blueboat-03"}'
```

MAVLink camera discovery probes `<camera-host>:8889` after bridge connection and every 60 seconds. On success it publishes `http://<camera-host>:8889/cam/whep`. The `yp-server` container must be able to reach that host and port. A failed probe does not erase an existing stream. The optional Camera Host field is sent as `camera_host`; when omitted, host-based `tcp:`, `tcpout:`, `udpout:`, and `udpbcast:` URLs can provide the host. Serial URLs, inbound/wildcard listeners, `0.0.0.0`, and `localhost` require an explicit camera host. The probe checks raw TCP reachability; the browser negotiates WHEP only when video is opened.

## Vehicle deconfliction

When enabled, detection runs every 0.5 seconds using horizontal great-circle distance plus altitude difference. A conflict occurs below the sum of the vehicles' safety radii. The lower-priority vehicle receives a temporary avoidance waypoint; its original command is preserved and re-dispatched after the conflict clears.

Priority order: `mob`, `search_grid`, `mission_plan`, then `waypoint`/`rtb`. Equal-priority conflicts use deterministic ordering. Default radii are 10 m for UAV/UAVF, 15 m for USV/UGV/UUV, and 20 m for YP. The API is:

```http
GET /api/deconfliction/settings
PUT /api/deconfliction/settings
GET /api/deconfliction/conflicts
```

## Advanced integrations

### UMAA

The default `sim-umaa` loopback bridge publishes heartbeat, `NavSatFix`, battery, and bridge-status messages, moves toward waypoints, and accepts waypoint, RTB, and SAR commands. Smoke-test it with:

```bash
docker compose up --build sim-umaa
python services/umaa_bridge/sim_umaa_smoke_test.py
```

The real RTI shell is enabled with:

```bash
docker compose --profile umaa-real up --build umaa-bridge
```

Fill in the vehicle-specific RTI topic map and generated DDS types. See [services/umaa_bridge/README.md](services/umaa_bridge/README.md) for loopback tuning and RTI variables.

Loopback tuning variables are `LOOPBACK_SPEED_MPS`, `LOOPBACK_TURN_RATE_DPS`, `LOOPBACK_ARRIVAL_RADIUS_M`, `LOOPBACK_BATTERY_DRAIN_PER_M`, and `LOOPBACK_BATTERY_DRAIN_PER_S`. RTI wiring variables include `RTI_DOMAIN_ID`, `RTI_QOS_FILE`, `RTI_SOURCE_GUID`, `RTI_COMMAND_TOPIC`, `RTI_ACK_TOPIC`, `RTI_STATUS_TOPIC`, `RTI_NAVSATFIX_TOPIC`, `RTI_BATTERY_TOPIC`, `RTI_HEARTBEAT_TOPIC`, `RTI_PUBLISHER_NAME`, and `RTI_SUBSCRIBER_NAME`.

### PX4/MAVROS

Enable the PX4 profile with:

```bash
docker compose --profile px4 up --build
```

The path is PX4 SITL -> MAVROS -> rosbridge -> `px4-yp-bridge` -> `yp-server`, with vehicle ID `px4-uav`. The bridge discovers `/mavros/...` topics through rosapi and keeps these core topics subscribed: `/mavros/state`, `/mavros/extended_state`, `/mavros/global_position/global`, `/mavros/global_position/compass_hdg`, `/mavros/global_position/rel_alt`, `/mavros/local_position/pose`, `/mavros/local_position/velocity_local`, `/mavros/battery`, `/mavros/imu/data`, `/mavros/home_position/home`, and `/mavros/gpsstatus/gps1/raw`.

Canonical aliases are published at `/vehicles/px4-uav/navsatfix`, `/vehicles/px4-uav/pose`, `/vehicles/px4-uav/battery`, and `/vehicles/px4-uav/heading`. All forwarded topics are ingested, written to InfluxDB, and shown in the message drawer.

### Scaling simulators

The compose file defines `sim-uav1` and `sim-uav2` with fixed IDs, so scaling those services without overriding `VEHICLE_ID` would create duplicate vehicle IDs. For additional vehicles, add services or run `sim_vehicle.py` instances with unique IDs. The older `sim-uav` service is commented out and should not be used as a scaling target.

## Message transport

<p align="center">
<img src="screenshots/messages.png" alt="Messages" height="300">
</p>

Native vehicle clients connect to:

```text
ws://<server-host>:8000/ws/vehicle/<vehicle_id>
```

Messages use ROS-shaped JSON, for example:

```json
{
  "vehicle_id": "uav-alpha",
  "vehicle_type": "uav",
  "topic": "/vehicles/uav-alpha/navsatfix",
  "type": "sensor_msgs/msg/NavSatFix",
  "stamp": 1778952000.25,
  "msg": { "latitude": 38.982, "longitude": -76.483, "altitude": 45.0 }
}
```

Commands return over the same socket. The YP rosbridge-like endpoint is `ws://<server-host>:8000/ws/rosbridge` and supports `publish`, `subscribe`, `unsubscribe`, and `command`. The PX4 profile uses a real `rosbridge_server` container before converting ROS messages to the native YP contract.

Supported ROS-shaped message families include `NavSatFix`, `Pose`, `PoseStamped`, `BatteryState`, `Imu`, `MultiDOFJointTrajectory`, `mavros_msgs/State`, and `mavros_msgs/GlobalPositionTarget`.

## Maps, GPS, and configuration

### YP GPS

The default simulated YP uses:

```yaml
GPS_MODE: sim
HOME_LAT: "38.989639"
HOME_LON: "-76.478643"
HOME_ALT: "2.0"
HEADING_DEG: "330"
SPEED_KNOTS: "3"
CIRCLE_LEFT_LON: "-76.487031"
CIRCLE_RIGHT_LON: "-76.479393"
CIRCLE_CW: "true"
```

For real NMEA GPS, use `GPS_MODE: serial`, set `SERIAL_PORT` and `BAUD_RATE`, and pass the device through to the container. The publisher provides YP `NavSatFix`, `Pose`, `BatteryState`, and heartbeat messages.

### Map tiles

Street Maps use OpenStreetMap; Satellite uses Esri World Imagery. The server proxies and caches visible tiles at:

```text
http://localhost:8000/tiles/osm/<z>/<x>/<y>.png
http://localhost:8000/tiles/earth/<z>/<x>/<y>.png
```

Cached tiles persist in `data/tile-cache/`. Check or clear the cache:

```bash
curl http://localhost:8000/api/tile-cache
rm -rf data/tile-cache
```

Runtime viewing caches only tiles requested by the active viewport. It does not bulk download or pre-seed public providers. Use `scripts/download_tiles.py` only with offline sources that explicitly permit preloading.

### Main environment variables

| Service | Variable | Default | Description |
| --- | --- | --- | --- |
| `yp-server` | `INFLUX_URL` | `http://influxdb:8086` | InfluxDB URL |
| `yp-server` | `INFLUX_ORG` / `INFLUX_BUCKET` | `yp` / `telemetry` | InfluxDB organization and bucket |
| `yp-server` | `INFLUX_TOKEN` | `yp-dev-token` | InfluxDB token |
| `yp-server` | `AUTH_DB_PATH` | `/data/auth/auth.db` | SQLite account database |
| `yp-server` | `JWT_SECRET` | development placeholder | JWT signing secret; replace outside development |
| `yp-server` | `JWT_EXPIRATION_MINUTES` | `1440` | Session lifetime |
| `yp-server` | `TILE_CACHE_DIR` | `/data/tile-cache` | Persistent tile cache |
| `yp-server` | `OSM_TILE_URL` / `EARTH_TILE_URL` | OpenStreetMap / Esri templates | Online tile sources |
| `yp-server` | `OSM_USER_AGENT` / `OSM_REFERER` | `YPGroundStation/0.1` / `http://localhost:8080/` | Street tile request identity |
| `yp-server` | `EARTH_USER_AGENT` / `EARTH_REFERER` | `YPGroundStation/0.1` / `http://localhost:8080/` | Satellite tile request identity |
| `yp-server` | `TILE_MAX_CACHE_AGE_SECONDS` | `31536000` | Maximum cached tile age |
| `yp-server` | `MAX_TILE_ZOOM` | `20` | Maximum proxy zoom |
| `yp-server` | `VEHICLE_TTL_SECONDS` | `30` | Stale vehicle threshold |
| `yp-server` | `HISTORY_MAX_POINTS` | `5000` | Position history limit |
| `yp-server` | `MESSAGE_RETENTION_SECONDS` | `600` | Message retention |
| `yp-server` | `MESSAGE_CLEANUP_INTERVAL_SECONDS` | `600` | Message cleanup interval |
| `yp-server` | `INFLUX_MAX_WRITE_HZ` | `5` | Influx write limit |
| `yp-server` | `RTB_STERN_DISTANCE_M` / `RTB_UPDATE_HZ` | `35.0` / `2.0` | RTB target and update rate |
| `yp-server` | `SAR_*` | See SAR tables | MOB/SAR tuning |
| `sim-*` | `VEHICLE_TYPE` | `uav` | `uav`, `uavf`, `usv`, `uuv`, or `ugv` |
| `sim-*` | `VEHICLE_ID` | auto | Stable vehicle ID |
| `sim-*` | `HOME_LAT` / `HOME_LON` | `38.9822` / `-76.4819` | Home/RTB position |
| `yp-gps` | `GPS_MODE` | `sim` | `sim` or `serial` |
| `yp-gps` | `SERIAL_PORT` / `BAUD_RATE` | `/dev/ttyUSB0` / `9600` | NMEA device settings |
| `px4-sitl-uav` | `PX4_HOME_LAT`, `PX4_HOME_LON`, `PX4_HOME_ALT` | `38.98490`, `-76.47880`, `45.0` | PX4 home |
| `px4-sitl-uav` | `PX4_SYS_AUTOSTART` / `PX4_SIM_MODEL` | `4001` / `gz_x500` | PX4 x500 setup |
| `px4-yp-bridge` | `VEHICLE_ID` | `px4-uav` | UI vehicle ID |
| `px4-yp-bridge` | `ROSBRIDGE_URL` | `ws://rosbridge:9090` | ROS bridge URL |
| `px4-yp-bridge` | `SETPOINT_HZ` | `5` | Setpoint rate |
| `px4-yp-bridge` | `AUTO_ARM_OFFBOARD` | `true` | Arm and request Offboard |
| `px4-yp-bridge` | `GLOBAL_SETPOINT_FRAME` | `6` | MAVROS coordinate frame |
| `px4-yp-bridge` | `DISCOVER_MAVROS_TOPICS` | `true` | Discover `/mavros/...` topics |

## Development

Backend:

```bash
cd services/server
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend:

```bash
cd web
npm install
npm run dev
```

Production frontend commands are `npm run build`, `npm run build:demo`, and `npm run preview`.

## Notes for real vehicles

- Keep vehicle IDs stable and unique.
- Prefer one WebSocket per vehicle and the native WebSocket contract for the smallest moving part count.
- Use a strong `JWT_SECRET` and unique administrator credentials.
- Validate waypoint commands on the vehicle side before forwarding to a Cube/Pixhawk.
- Keep an independent manual RC/safety-pilot path.
- For camera discovery, ensure the `yp-server` container can reach the vehicle camera host on TCP port 8889.
