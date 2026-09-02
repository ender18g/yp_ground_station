# TRIDENT YP
### Telemetry, Remote Intelligence, Data, Electronic Navigation, and Tasking — Yard Patrol

![TRIDENT YP screenshot](screenshots/screen1.png)

Shipboard ground station for a Naval Academy Yard Patrol craft. The stack collects telemetry from USVs, UAVs, UUVs, a YP GPS feed, and an optional PX4/MAVROS UAV simulation, logs ROS-shaped messages to InfluxDB, and serves a local-first React/Leaflet map interface for monitoring and command.

## What Is Included

- `yp-server`: FastAPI service with native vehicle WebSockets, a lightweight rosbridge-compatible WebSocket, REST APIs, on-demand map tile caching, command routing, automatic vehicle deconfliction, InfluxDB logging, and SQLite/JWT account authorization.
- `web`: React + TypeScript + Leaflet UI with vehicle markers (UAV, USV, UUV, UGV, YP), headings, altitude labels, recent trails, YP range rings, hideable map layers, RTB commands, click-to-waypoint commands, a live message drawer, a visual waypoint planner tab, a YP role override, view-only mode, login, admin user management, and deconfliction settings.
- `sim-vehicle`: Lightweight configurable simulated UAV, USV, UUV, or UGV container. Publishes heartbeat, `NavSatFix`, `Pose`, `BatteryState`, and `MultiDOFJointTrajectory` messages at 5 Hz. Supports full SAR mission execution and temporary deconfliction waypoint detours from the server.
- `sim-umaa`: Lightweight UMAA loopback vehicle for testing the ground-station workflow before real DDS topics are available. Publishes heartbeat, `NavSatFix`, `BatteryState`, and bridge-status messages, accepts waypoint/RTB/SAR commands, and simulates motion toward the received target.
- `yp-gps`: YP GPS publisher. Runs in simulated mode near the US Naval Academy or reads NMEA GPS data from a serial port.
- `arducopter_ws_bridge`: Hardware bridge that connects a real ArduPilot/MAVLink vehicle (Cube, Pixhawk, etc.) to the ground station over a WebSocket. Supports SAR mission dispatch.
- `px4-sitl-uav`: Optional profile-gated PX4 SITL multicopter simulation.
- `mavros`, `ros-master`, and `rosbridge`: Optional ROS/MAVROS path used by the PX4 UAV simulation.
- `px4-yp-bridge`: Optional bridge that discovers and subscribes to MAVROS topics through rosbridge, forwards MAVROS messages into TRIDENT YP, and translates YP waypoint/RTB commands back to MAVROS/PX4.
- `umaa-bridge`: RTI Connext DDS bridge shell for a real UMAA vehicle once the DDS topic/type map is known.
- `influxdb`: Time-series storage for telemetry and command messages.
- `companion_vehicle_software`: Scripts and containers that run on the companion computer of a mobile vehicle.

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

The default compose file starts two simulated UAVs, one simulated USV, one simulated UUV, and a simulated YP GPS source located near the Severn River off the US Naval Academy.

## Accounts And Permissions

The web UI requires a username and password. On the first server startup, it creates a default administrator account:

```text
Username: admin
Password: admin
```

Change this password immediately after first login. The default credentials are for initial local setup only and must not be used on a reachable or operational network.

Administrators can open **User Management** from the users icon in the top bar. The icon is visible only to accounts with the `manage_users` permission. The panel can create and delete accounts, reset passwords, apply a role preset, and save a custom combination of individual permissions.

### Role Presets

| Role | Capabilities |
| --- | --- |
| `view_only` | Read telemetry and vehicle status |
| `waypoint_command` | View permissions plus waypoints, RTB, mode changes, and SAR cancellation |
| `mission_planning` | Waypoint permissions plus mission creation, upload, and search grids |
| `man_overboard` | Mission planning permissions plus MOB dispatch |
| `admin` | All operational permissions plus settings, connections, video stream, and user management |

Custom permission sets can combine any of the following permissions: `read_telemetry`, `read_vehicle_status`, `send_waypoint`, `send_rtb`, `set_vehicle_mode`, `cancel_sar`, `create_mission`, `upload_mission`, `search_grid`, `trigger_mob`, `manage_sitl`, `manage_settings`, `manage_video_streams`, and `manage_users`.

The server enforces permissions on protected REST endpoints and commands received through the UI WebSocket; hiding an action in the UI is not the authorization mechanism.

### Account Persistence

The default Compose configuration stores accounts in SQLite at [data/auth/auth.db](data/auth/auth.db) on the host, mounted as `/data/auth/auth.db` in `yp-server`. Accounts therefore persist across `docker compose up --build`, container recreation, and `docker compose down` followed by another `docker compose up`.

The database contains account metadata and password hashes. Keep it local and back it up when needed; it is excluded from Git. Deleting `data/auth/auth.db` deliberately resets the account store and causes the server to create the initial `admin` / `admin` account again.

The default mount is configured in `docker-compose.yml`:

```yaml
services:
  yp-server:
    environment:
      AUTH_DB_PATH: /data/auth/auth.db
    volumes:
      - ./data/auth:/data/auth
```

Set a unique `JWT_SECRET` for any non-development deployment.

## UMAA Bridge

The repository now includes a UMAA bridge path for testing a future RTI Connext DDS vehicle alongside the existing MAVLink and PX4 adapters.

### Simulated UMAA Vehicle

The default compose stack includes `sim-umaa`, a loopback vehicle that behaves like a moving USV and is meant for local testing before the real DDS topic map exists.

It starts with:

```bash
docker compose up --build sim-umaa
```

The simulated vehicle can be smoke-tested with:

```bash
python services/umaa_bridge/sim_umaa_smoke_test.py
```

That smoke test connects to `sim-umaa`, sends a waypoint, watches the simulated telemetry move, and then sends RTB.

### Real UMAA Bridge

When the real UMAA vehicle arrives, enable the `umaa-real` profile and run `umaa-bridge`. At that point you will fill in the RTI topic names and any generated DDS package details for the vehicle.

```bash
docker compose --profile umaa-real up --build umaa-bridge
```

The UMAA bridge is intentionally split into a simulation-friendly loopback path and a real DDS adapter shell so the same YP websocket contract can be exercised now and reused later without changing the UI or server command routing.

## PX4/MAVROS UAV Simulation

The PX4 UAV path is intentionally separate from the existing `sim-vehicle` containers. The repo includes the PX4 SITL container, `ros-master`, `mavros`, `rosbridge`, and `px4-yp-bridge` services for that path.

The PX4 setup is intended to run with the `px4` compose profile enabled:

```bash
docker compose --profile px4 up --build
```

The PX4 path connects PX4 SITL to MAVROS over MAVLink, exposes the ROS graph over WebSocket, and forwards MAVROS telemetry into `yp-server` as vehicle `px4-uav`.

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

Every forwarded topic is written into the ground station under `/vehicles/px4-uav/mavros/...`, ingested by `yp-server`, written to InfluxDB, and broadcast to the web UI message drawer.

## Demo Mode

The UI also supports a static demo mode for local previews and screenshots.

Open the app on `/demo`, add `?demo=true` to any URL, or build the frontend with `VITE_STATIC_DEMO=true`.

In demo mode the UI renders simulated vehicles locally instead of connecting to the live server.

## Scaling Existing Simulated Vehicles

To stress test with more of the lightweight simulated vehicles:

```bash
docker compose up --build --scale sim-uav=10 --scale sim-usv=4 --scale sim-uuv=3
```

Each simulator derives a unique ID from its container hostname unless `VEHICLE_ID` is explicitly set.

## Vehicle Deconfliction

The server can automatically separate vehicles whose reported three-dimensional positions conflict. Detection runs every 0.5 seconds and combines great-circle horizontal distance with altitude difference. A conflict occurs when the vehicles are closer than the sum of their individual safety radii. With the default 10 m UAV radius, two UAVs deconflict below 20 m separation.

When a conflict occurs, the vehicle with lower mission priority is temporarily sent to an avoidance waypoint away from the higher-priority vehicle. Its original command is preserved and automatically re-dispatched after the conflict clears. This applies to the built-in `sim-` vehicles as well as bridge-connected vehicles that accept standard waypoint commands.

Mission priority is:

1. `mob`
2. `search_grid`
3. `mission_plan`
4. `waypoint` and `rtb`

Equal-priority conflicts use a deterministic ordering and divert one vehicle; operators should avoid scheduling overlapping equal-priority missions where possible.

### RTB Stern Follow

RTB is a persistent stern-follow mode. The server continuously computes a moving target directly aft of the YP's current heading and sends updated waypoint setpoints at the configured RTB update rate. Vehicles approaching from the bow or beam are first routed outside the combined safety envelope, around an aft quarter, and then into the stern station; they do not take a direct path across the YP. RTB remains active until the vehicle is retasked or the RTB-follow task is canceled.

### Configuration

An administrator can open **Settings** and select the **Deconfliction** tab, positioned between **Display** and **Man Overboard**, to enable the feature and set the global and per-vehicle safety radii. The default radii are 10 m for UAV/UAVF, 15 m for USV/UGV/UUV, and 20 m for the YP. Settings persist in the SQLite database alongside account data and are loaded when `yp-server` starts.

The following API endpoints are also available:

```http
GET /api/deconfliction/settings
PUT /api/deconfliction/settings
GET /api/deconfliction/conflicts
```

Updating settings requires the `manage_settings` permission. The conflicts endpoint returns the currently detected lower-priority and higher-priority vehicle pairs.

The Settings modal persists its Display, Vessel, and Man Overboard controls in the SQLite database. This includes trail duration, YP range rings, message retention, RTB update rate, stern distance, YP role, MOB track length, corridor width, swath width, search altitude, takeoff altitude, and climb speed. These values are loaded when the server starts and can also be read or updated through `GET` and `PUT /api/settings`.

## Web UI

The map shows:

- Green USV markers
- Orange UAV markers
- Yellow UUV markers
- Amber UGV markers (ground rovers)
- Gray YP marker
- Heading arrow for each vehicle
- Altitude beside each marker
- Adjustable recent trail duration
- Hover popup with telemetry
- Click modal with `RTB`, `Waypoint`, flight mode control, and stream video actions (hidden for non-commandable vehicles in view-only mode)
- Hideable map layer/source menu opened with the layer icon
- Optional YP range rings at 50 m, 100 m, and 200 m
- Live message drawer opened with the message icon
- Vehicle Connections panel (cable icon) to connect ArduPilot SITL instances or RFD-900 radios at runtime
- SAR mission patterns overlaid on the map when a grid search or MOB mission is dispatched; click the filled start dot to open a popup and clear the pattern manually
- YP role override in the Settings menu for assigning another connected vessel as the mother ship

The top bar also contains a **Waypoint Planner** tab (chart icon) for visual top-down mission planning, and a **View only** badge is shown when the UI is loaded in view-only mode (see [View-Only Mode](#view-only-mode)).

The Settings menu controls trail duration and YP range rings. The message drawer shows the newest live messages and the latest per-topic messages included in the initial vehicle snapshot, which helps inspect the extra MAVROS topics from `px4-uav`.

### Vehicle Modal Features

When clicking a vehicle marker, a draggable modal window appears with the following features:

- **Position and Telemetry**: Real-time latitude, longitude, altitude, heading, battery percentage, and SAR mission status
- **Ship Reference Frame**: For vehicles with a selected mother ship, displays forward/left/up distances and radial distance in ship-fixed coordinates (FLU convention)
- **Commands**: RTB button to return the vehicle to the mother ship, Waypoint button to set a single target waypoint
- **Flight Mode Control**: For ArduPilot and PX4 vehicles, a Settings button expands to show vehicle-type-specific flight modes. Click any mode to change the vehicle's current flight mode
- **Video Stream**: If the vehicle has video streams enabled, a Stream Video button opens a WHEP WebRTC player
- **Color Picker**: A Color button toggles a palette to customize the vehicle marker colour on the map

### Waypoint Planner

The visual **Waypoint Planner** tab provides a dedicated map mode for building waypoint missions before dispatch:

- **Dynamic Scaling**: Vehicle icons scale smoothly when zooming in and out, matching the zoom behavior of Global Map mode
- **Map Persistence**: When switching between Global Map and Mission Planner modes, the map center position and zoom level are preserved
- **Waypoint Editing**: Left-click the map to add waypoints; drag to move; click a waypoint to edit altitude and parameters
- **Waypoint Reordering**: Move waypoints up or down in the sequence using the waypoint list panel
- **Mission Upload**: Select a target vehicle and click Upload Mission to arm the vehicle, set AUTO mode, and start the waypoint sequence
- **Optional Force Guided**: Add a GUIDED waypoint at the mission end to hold position after completing all waypoints
- **File Export**: Download missions as QGC Plan or .wpl format; upload previously saved missions

## Video Streams

The UI shows the **Stream Video** action when a vehicle payload includes `video.enabled=true` and at least one entry in `video.streams`.

Each stream entry is expected to look like:

```json
{ "label": "Bow Camera", "url": "http://<whep-host>/<stream-id>/whep" }
```

The current frontend player negotiates WebRTC using WHEP by POSTing SDP offers directly to the selected stream `url`.

The backend video stream API remains available for storing per-vehicle stream metadata (`stream_id`, `source_rtsp_url`, `playback_url`). Those API-managed fields are useful for control/config workflows, but the current UI video modal consumes `video.streams` from live vehicle data.

### Video Stream API

```http
GET    /api/video/streams                         -> list stream mappings (safe fields)
PUT    /api/video/streams/{vehicle_id}            -> create/update mapping
DELETE /api/video/streams/{vehicle_id}            -> remove mapping
GET    /api/video/streams?include_sources=true    -> include RTSP source URLs (admin use)
```

Example update call:

```bash
curl -X PUT http://localhost:8000/api/video/streams/blueboat-03 \
  -H 'Content-Type: application/json' \
  -d '{"source_rtsp_url":"rtsp://user:pass@10.0.0.23:554/stream1","stream_id":"blueboat-03"}'
```

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

Leave the **Vehicle ID** field empty to auto-derive an ID from the connection URL (e.g. `vehicle-localhost-5760`), or enter a custom ID.

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
| `rtb` | Starts server-side return-to-boat follow (continuous waypoint updates to a stern offset from the current YP vessel) |
| `search_grid` | Runs a streaming boustrophedon lawnmower mission (carrot-chase waypoints) |
| `mob` | Runs a streaming curved track-following MOB mission (carrot-chase waypoints) |
| `cancel_sar` | Cancels an in-progress streaming SAR mission |
| `mission_plan` | Uploads a full waypoint sequence and optionally arms + starts the vehicle in AUTO mode |
| `set_mode` | Changes the vehicle's flight mode (e.g., "AUTO", "RTL", "LOITER") |

For `search_grid` and `mob`, the bridge now executes missions in streaming mode (one waypoint at a time) instead of full mission upload. During streaming, the mission worker keeps forwarding position telemetry so vehicle updates and map motion remain live.

For `rtb`, the server does not send a one-shot RTL command. It continuously computes a dynamic stern target behind the selected YP vessel and pushes updates until RTB is canceled or the vehicle is retasked. Built-in `sim-*` vehicles and MAVLink bridges use a dedicated follow mode after the aft approach, matching the YP's estimated speed and heading with bounded position correction instead of chasing the moving stern point. The UMAA loopback accepts the command through its waypoint fallback; the real UMAA DDS adapter remains a command-schema placeholder until its vehicle-specific DDS types are integrated.

For `set_mode`, the vehicle modal's Settings button expands to show a grid of available modes for the vehicle type. Supported modes are:

- **ArduPilot Vehicles (UAV/USV/UGV)**: STABILIZE, ACRO, ALT_HOLD, AUTO, GUIDED, LOITER, RTL, CIRCLE, LAND, DRIFT, SPORT, FLIP, AUTOTUNE, POSHOLD
- **PX4 Vehicles (UAVF)**: MANUAL, ALTITUDE_CONTROL, POSITION_CONTROL, AUTO, OFFBOARD, EMERGENCY

The mode selector automatically appears only for vehicles that have defined modes, and mode changes are sent to all bridge types (SITL, hardware, and distributed bridges).

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

Click the **MOB** button in the top bar and confirm. The MOB modal now includes a **Dispatch vehicle** dropdown so you can choose exactly which connected vehicle receives the mission. UGV vehicles and the YP itself are excluded from the dropdown. The server:

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

The `arducopter_ws_bridge` service supports the same `search_grid`, `mob`, and `set_mode` command types. When a command is routed to a hardware bridge vehicle, `arducopter_ws_bridge.py` receives it over WebSocket and executes it in streaming carrot-chase mode over direct MAVLink. The pattern overlay is shown on the map at dispatch time. Flight mode changes are sent immediately to the connected vehicle.

## RFD-900 / Telemetry Radio Support

The ground station can connect to a real MAVLink vehicle over an RFD-900 (or any serial telemetry radio) through the browser Connections panel.

### Windows Host TCP Relay

Because Docker on Windows cannot directly access COM ports, a host-side relay script bridges the serial radio to a TCP port that the `yp-server` container can reach:

```bash
pip install pyserial
python services/com_tcp_relay.py --port COM12 --baud 57600 --tcp-port 5762
```

The relay opens the COM port and listens for a single TCP connection. When `yp-server` connects, bytes flow bidirectionally between Docker and the radio. The relay keeps the serial port open and accepts a new TCP connection automatically each time Docker reconnects.

### Connecting in the UI

Open the **Connections** panel (cable icon), switch to the **RFD-900** tab. Select a serial port from the dropdown (populated by the `/api/serial-ports` endpoint), set a baud rate, and click **Connect** — the server connects using the relay URL `tcp:host.docker.internal:5762` automatically.

Alternatively, use the **Network** tab and enter the relay URL directly:

```
tcp:host.docker.internal:5762
```

### Serial Device Passthrough (Linux / native Docker)

On Linux the radio can be passed directly to the server container without a relay. Uncomment the `devices:` block in `docker-compose.yml` under `yp-server`:

```yaml
devices:
  - /dev/ttyUSB0:/dev/ttyUSB0
```

Then connect using the serial URL in the Network tab:

```
serial:/dev/ttyUSB0:57600
```

### Serial Port API

```http
GET /api/serial-ports   → list serial ports visible to the server container
```

## Bridge Utilities

The repository also includes a few standalone bridge scripts that are useful outside the main compose stack:

- `services/server/app/main.py`: FastAPI SITL bridge with support for waypoints, RTB follow, SAR missions, mission upload, and flight mode changes
- `services/telemetry_radio_bridge.py`: Direct serial-radio to YP WebSocket bridge with MAVLink telemetry forwarding and command routing
- `services/arducopter_ws_bridge/arducopter_ws_bridge.py`: ArduPilot WebSocket bridge with SAR mission handling and flight mode control
- `services/px4_mavros_bridge/px4_mavros_bridge.py`: ROS/MAVROS to YP bridge with PX4-specific mode mapping
- `services/umaa_bridge/umaa_bridge.py`: RTI Connext DDS bridge for UMAA vehicles
- `blueboat_piScripts/blueboat_bridge.py`: BlueBoat/ArduPilot bridge with SAR mission handling and flight mode support
- `blueboat_piScripts/simplified_bridge.py`: Minimal MAVLink-to-YP telemetry bridge example

## View-Only Mode

The UI can be opened in view-only mode by navigating to the `/view` path or appending `?view=true` to any URL:

```
http://localhost:8080/view
http://localhost:8080/?view=true
```

In view-only mode:

- Live telemetry updates and the map operate normally.
- Commands (RTB, Waypoint, SAR) are **blocked** for real hardware vehicles. The RTB and Waypoint buttons are hidden in the vehicle modal.
- Simulated vehicles (those whose IDs start with `sim-`) remain fully commandable.
- The Vehicle Connections panel (cable icon) is hidden; new connections cannot be added.
- A **View only** badge is displayed in the top status bar.

This is useful for displaying the situational picture on secondary screens or for observers who should not be able to send commands to real vehicles.

## Waypoint Planner

A visual **Waypoint Planner** tab is available in the top bar (chart/ruler icon). It provides a top-down planning view for building waypoint routes before dispatching them, and it can send ship-relative trajectories using the current YP position for spatial context.

### Mission Planner Features

- **Dynamic Vehicle Scaling**: Vehicle icons automatically scale when zooming the map, providing visual consistency with the Global Map mode
- **Map Persistence**: Map center and zoom level persist when switching between Global Map and Mission Planner modes
- **Interactive Waypoints**: Left-click to add waypoints, drag to reposition, click to edit details
- **Sequence Management**: Reorder waypoints or remove individual waypoints from the mission
- **Vehicle Selection**: Choose which connected vehicle receives the mission upload
- **Parameter Configuration**: Set altitude, acceptance radius, hold time, and yaw for each waypoint
- **Mission File Support**: Export missions in QGC Plan (.plan) or .wpl format; import previously saved missions
- **One-Click Upload**: Send the complete mission to the selected vehicle with optional auto-arm and mission start

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

On the server, this command starts return-to-boat follow mode for that vehicle. The server continuously emits RTB updates (`source="rtb_follow"`) toward a stern offset from the current YP vessel at `rtb_update_hz` until RTB is canceled or the vehicle is retasked.

Bridge implementations treat `source="rtb_follow"` as a lightweight stream and skip repeated mode/arm transitions so telemetry remains responsive while tracking the moving stern target.

For the PX4/MAVROS vehicle, behavior still depends on bridge configuration and PX4 state. PX4 may reject arming, Offboard, or streamed setpoints if its simulated sensors, EKF state, preflight checks, or failsafe state are not ready. Check the `mavros`, `px4-sitl-uav`, and `px4-yp-bridge` logs when a command is acknowledged by the UI but not acted on by PX4.

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
HOME_LAT: "38.989639"
HOME_LON: "-76.478643"
HEADING_DEG: "330"
SPEED_KNOTS: "3"
CIRCLE_LEFT_LON: "-76.487031"
CIRCLE_RIGHT_LON: "-76.479393"
CIRCLE_CW: "true"
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

In simulated mode, the YP starts at latitude `38.989639`, longitude `-76.478643`, heading `330` degrees, and moves at `3` knots unless those environment variables are changed.

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

The runtime app does not bulk download, pre-seed, or scan map areas. It caches only tiles requested by the active map viewport. OpenStreetMap's public tile service allows normal interactive viewing and local caching according to HTTP cache headers, but prohibits bulk downloads and offline prefetch features. The separate `scripts/download_tiles.py` helper exists for offline tile sources that explicitly permit preloading.

## Configuration

Useful environment variables:

| Service | Variable | Default | Description |
| --- | --- | --- | --- |
| `yp-server` | `INFLUX_URL` | `http://influxdb:8086` | InfluxDB URL |
| `yp-server` | `INFLUX_ORG` | `yp` | InfluxDB org |
| `yp-server` | `INFLUX_BUCKET` | `telemetry` | InfluxDB bucket |
| `yp-server` | `INFLUX_TOKEN` | `yp-dev-token` | InfluxDB token |
| `yp-server` | `AUTH_DB_PATH` | `/data/auth/auth.db` in Compose | SQLite account database path; host-mounted at `data/auth/` by default |
| `yp-server` | `JWT_SECRET` | `yp-dev-secret-change-me` | JWT signing secret; set a unique long random value for any non-development deployment |
| `yp-server` | `JWT_EXPIRATION_MINUTES` | `1440` | JWT session lifetime in minutes |
| `yp-server` | `TILE_CACHE_DIR` | `/data/tile-cache` | Persistent on-demand map tile cache |
| `yp-server` | `OSM_TILE_URL` | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` | Street tile source URL template |
| `yp-server` | `EARTH_TILE_URL` | `https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}` | Satellite tile source URL template |
| `yp-server` | `MAX_TILE_ZOOM` | `20` | Maximum tile zoom level served by the proxy |
| `yp-server` | `VEHICLE_TTL_SECONDS` | `30` | Seconds before an unheard vehicle is considered stale |
| `yp-server` | `HISTORY_MAX_POINTS` | `5000` | Maximum position history points kept per vehicle |
| `yp-server` | `SAR_CORRIDOR_HALF_WIDTH_M` | `50.0` | MOB search corridor half-width in metres |
| `yp-server` | `SAR_SWATH_M` | `20.0` | SAR lane spacing in metres |
| `yp-server` | `SAR_ALTITUDE_M` | `30.0` | SAR search altitude in metres |
| `yp-server` | `SAR_MOB_TRACK_SECONDS` | `120.0` | Default YP history window used for MOB mission generation |
| `yp-server` | `SAR_TAKEOFF_ALT_M` | `30.0` | SAR takeoff altitude in metres |
| `yp-server` | `SAR_CLIMB_SPEED_MS` | `8.0` | SAR climb speed in m/s |
| `yp-server` | `RTB_STERN_DISTANCE_M` | `35.0` | Distance behind YP heading used as RTB-follow target |
| `yp-server` | `RTB_UPDATE_HZ` | `2.0` | Default RTB-follow waypoint update rate |
| `sim-*` | `VEHICLE_TYPE` | `uav` | `uav`, `uavf`, `usv`, `uuv`, or `ugv` |
| `sim-*` | `VEHICLE_ID` | auto | Optional fixed vehicle ID |
| `sim-*` | `HOME_LAT` | `38.9822` | RTB/home latitude |
| `sim-*` | `HOME_LON` | `-76.4819` | RTB/home longitude |
| `yp-gps` | `GPS_MODE` | `sim` | `sim` or `serial` |
| `yp-gps` | `SERIAL_PORT` | `/dev/ttyUSB0` | NMEA GPS serial device |
| `yp-gps` | `HEADING_DEG` | `330` | Simulated YP heading in degrees |
| `yp-gps` | `SPEED_KNOTS` | `3` | Simulated YP speed |
| `yp-gps` | `CIRCLE_LEFT_LON` | *(unset)* | Left longitude bound for circular YP movement in sim mode |
| `yp-gps` | `CIRCLE_RIGHT_LON` | *(unset)* | Right longitude bound for circular YP movement in sim mode |
| `yp-gps` | `CIRCLE_CW` | `false` | `true` to start the circular track clockwise |
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
- Set a strong `JWT_SECRET` and use unique administrator credentials before putting this on anything other than a trusted local shipboard network.
- Validate waypoint commands on the vehicle side before forwarding to the Cube/Pixhawk.
- Keep a manual RC/safety pilot path independent of the web UI.
