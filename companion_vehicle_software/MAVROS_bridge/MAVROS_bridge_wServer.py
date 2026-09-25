from __future__ import annotations

import asyncio
import json
import math
import os
import socket
import threading
import time
import traceback
from pathlib import Path
from typing import Any
import sys

from aiohttp import web
import websockets

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from yp_common import behaviors_agnostic
from yp_common.geometry import distance_m as _distance_m

CONFIG_PATH = Path("config.json")

# --- DEFAULT CONFIGURATION ---
DEFAULT_CONFIG = {
    "server_ws_url": os.getenv("SERVER_WS_URL", "ws://192.168.0.174:8000/ws/vehicle"),
    "vehicle_id": os.getenv("VEHICLE_ID", "px4-uav"),
    "vehicle_type": os.getenv("VEHICLE_TYPE", "uav"),
    "rosbridge_url": os.getenv("ROSBRIDGE_URL", "ws://127.0.0.1:9090"),
    "setpoint_hz": float(os.getenv("SETPOINT_HZ", "5")),
    "auto_arm_offboard": os.getenv("AUTO_ARM_OFFBOARD", "true").lower() in {"1", "true", "yes", "on"},
    "global_setpoint_frame": int(os.getenv("GLOBAL_SETPOINT_FRAME", "6")),
    "discover_mavros_topics": os.getenv("DISCOVER_MAVROS_TOPICS", "true").lower() in {"1", "true", "yes", "on"},
    "web_port": int(os.getenv("WEB_PORT", "8083")),
}

config: dict[str, Any] = {}
reconnect_event = asyncio.Event()

system_status: dict[str, Any] = {
    "ros_connected": False,
    "ros_status": "Connecting...",
    "ws_connected": False,
    "ws_status": "Connecting...",
    "flight_mode": "UNKNOWN",
    "armed": False,
    "gps_status": "No Fix",
    "satellites": 0,
    "last_hb_time": 0.0,
    "discovered_topics": {},
}

DEFAULT_SUBSCRIPTIONS: list[tuple[str, str]] = [
    ("/mavros/state", "mavros_msgs/State"),
    ("/mavros/extended_state", "mavros_msgs/ExtendedState"),
    ("/mavros/global_position/global", "sensor_msgs/NavSatFix"),
    ("/mavros/global_position/compass_hdg", "std_msgs/Float64"),
    ("/mavros/global_position/rel_alt", "std_msgs/Float64"),
    ("/mavros/local_position/pose", "geometry_msgs/PoseStamped"),
    ("/mavros/local_position/velocity_local", "geometry_msgs/TwistStamped"),
    ("/mavros/battery", "sensor_msgs/BatteryState"),
    ("/mavros/imu/data", "sensor_msgs/Imu"),
    ("/mavros/home_position/home", "mavros_msgs/HomePosition"),
    ("/mavros/gpsstatus/gps1/raw", "mavros_msgs/GPSRAW"),
]

SETPOINT_TOPIC = "/mavros/setpoint_raw/global"
SETPOINT_TYPE = "mavros_msgs/GlobalPositionTarget"


# --- CONFIG MANAGEMENT ---

def load_config() -> dict[str, Any]:
    if CONFIG_PATH.is_file():
        try:
            with open(CONFIG_PATH, "r") as f:
                saved = json.load(f)
                return {**DEFAULT_CONFIG, **saved}
        except Exception as e:
            print(f"[WARN] Error loading config.json, using defaults: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(new_config: dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(new_config, f, indent=2)


# --- WEB DIAGNOSTICS & CONFIG SERVER ---

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>MAVROS Bridge - Diagnostics & Config</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; padding: 20px; max-width: 650px; margin: 0 auto; }}
        h2 {{ color: #38bdf8; border-bottom: 2px solid #334155; padding-bottom: 10px; margin-bottom: 15px; margin-top: 25px; }}
        .diag-card {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 15px; margin-bottom: 20px; }}
        .diag-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 10px; }}
        .diag-item {{ background: #0f172a; padding: 10px; border-radius: 6px; border: 1px solid #1e293b; }}
        .diag-label {{ font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px; }}
        .diag-value {{ font-size: 1.05rem; font-weight: 600; margin-top: 4px; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.85rem; font-weight: 600; }}
        .badge-online {{ background: #065f46; color: #34d399; }}
        .badge-offline {{ background: #881337; color: #fda4af; }}
        label {{ display: block; margin-top: 15px; font-weight: 600; font-size: 0.9rem; color: #94a3b8; }}
        input, select {{ width: 100%; padding: 10px; margin-top: 5px; border-radius: 6px; border: 1px solid #475569; background: #1e293b; color: white; box-sizing: border-box; font-size: 1rem; }}
        .checkbox-label {{ display: flex; align-items: center; gap: 10px; margin-top: 15px; font-weight: 600; color: #f8fafc; cursor: pointer; }}
        .checkbox-label input {{ width: auto; margin-top: 0; }}
        button {{ width: 100%; margin-top: 25px; padding: 12px; background: #2563eb; color: white; border: none; border-radius: 6px; font-size: 1rem; font-weight: bold; cursor: pointer; }}
        button:hover {{ background: #1d4ed8; }}
        .topic-table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.85rem; }}
        .topic-table th, .topic-table td {{ text-align: left; padding: 8px; border-bottom: 1px solid #334155; }}
        .topic-table th {{ color: #38bdf8; font-weight: 700; background: #0f172a; }}
        .topic-list-container {{ max-height: 250px; overflow-y: auto; border: 1px solid #334155; border-radius: 6px; margin-top: 10px; }}
    </style>
</head>
<body>
    <h2>System Diagnostics</h2>
    <div class="diag-card">
        <div class="diag-grid">
            <div class="diag-item">
                <div class="diag-label">ROS 2 / Rosbridge</div>
                <div class="diag-value"><span id="ros_status" class="badge badge-offline">Offline</span></div>
            </div>
            <div class="diag-item">
                <div class="diag-label">GCS WebServer</div>
                <div class="diag-value"><span id="ws_status" class="badge badge-offline">Offline</span></div>
            </div>
            <div class="diag-item">
                <div class="diag-label">Flight Mode</div>
                <div class="diag-value" id="flight_mode" style="color: #38bdf8;">UNKNOWN</div>
            </div>
            <div class="diag-item">
                <div class="diag-label">GPS Status</div>
                <div class="diag-value" id="gps_status" style="color: #f59e0b;">No Fix</div>
            </div>
        </div>
    </div>

    <h2>MAVROS Configuration</h2>
    <form method="POST" action="/save">
        <label>Vehicle ID</label>
        <input type="text" name="vehicle_id" value="{vehicle_id}" required>

        <label>Vehicle Type</label>
        <select name="vehicle_type">
            <option value="uav" {s_uav}>uav (Aerial Drone)</option>
            <option value="usv" {s_usv}>usv (Surface Vessel)</option>
            <option value="ugv" {s_ugv}>ugv (Ground Rover)</option>
            <option value="uuv" {s_uuv}>uuv (Submarine)</option>
        </select>

        <label>GCS Server WebSocket URL</label>
        <input type="text" name="server_ws_url" value="{server_ws_url}" required>

        <label>ROSBridge WebSocket URL</label>
        <input type="text" name="rosbridge_url" value="{rosbridge_url}" required>

        <label>Setpoint Stream Rate (Hz)</label>
        <input type="number" step="0.1" name="setpoint_hz" value="{setpoint_hz}" required>

        <label>Global Setpoint Frame (MAV_FRAME)</label>
        <select name="global_setpoint_frame">
            <option value="5" {f_5}>5 - MAV_FRAME_GLOBAL_INT</option>
            <option value="6" {f_6}>6 - MAV_FRAME_GLOBAL_RELATIVE_ALT_INT (Default)</option>
            <option value="11" {f_11}>11 - MAV_FRAME_GLOBAL_TERRAIN_ALT_INT</option>
        </select>

        <label class="checkbox-label">
            <input type="checkbox" name="auto_arm_offboard" value="true" {c_auto_arm}>
            Auto-Arm & Enter OFFBOARD mode on Setpoint
        </label>

        <label class="checkbox-label">
            <input type="checkbox" name="discover_mavros_topics" value="true" {c_discover}>
            Automatically Discover Active MAVROS Topics via ROS API
        </label>

        <button type="submit">Save & Reconnect Bridge</button>
    </form>

    <h2>Active MAVROS Topics (<span id="topic_count">0</span>)</h2>
    <div class="topic-list-container">
        <table class="topic-table">
            <thead>
                <tr>
                    <th>Topic Name</th>
                    <th>ROS Message Type</th>
                </tr>
            </thead>
            <tbody id="topic_rows">
                <tr><td colspan="2" style="color: #94a3b8; text-align: center;">Discovering topics...</td></tr>
            </tbody>
        </table>
    </div>

    <script>
        async function fetchStatus() {{
            try {{
                const res = await fetch('/api/status');
                const data = await res.json();
                
                const rosElem = document.getElementById('ros_status');
                rosElem.innerText = data.ros_status;
                rosElem.className = 'badge ' + (data.ros_connected ? 'badge-online' : 'badge-offline');

                const wsElem = document.getElementById('ws_status');
                wsElem.innerText = data.ws_status;
                wsElem.className = 'badge ' + (data.ws_connected ? 'badge-online' : 'badge-offline');

                document.getElementById('flight_mode').innerText = data.flight_mode + (data.armed ? ' (ARMED)' : ' (DISARMED)');
                
                const gpsText = data.gps_status + (data.satellites > 0 ? ` (${{data.satellites}} Sats)` : '');
                document.getElementById('gps_status').innerText = gpsText;

                // Render Discovered Topics
                const topics = data.discovered_topics || {{}};
                const keys = Object.keys(topics).sort();
                document.getElementById('topic_count').innerText = keys.length;
                
                if (keys.length > 0) {{
                    let html = '';
                    for (const t of keys) {{
                        html += `<tr><td><code>${{t}}</code></td><td><code>${{topics[t]}}</code></td></tr>`;
                    }}
                    document.getElementById('topic_rows').innerHTML = html;
                }}
            }} catch (e) {{ console.error("Failed fetching status", e); }}
        }}
        setInterval(fetchStatus, 1000);
        fetchStatus();
    </script>
</body>
</html>
"""


async def handle_index(request: web.Request) -> web.Response:
    frame = str(config.get("global_setpoint_frame", 6))
    v_type = str(config.get("vehicle_type", "uav"))

    html = HTML_TEMPLATE.format(
        vehicle_id=config.get("vehicle_id", "px4-uav"),
        s_uav="selected" if v_type == "uav" else "",
        s_usv="selected" if v_type == "usv" else "",
        s_ugv="selected" if v_type == "ugv" else "",
        s_uuv="selected" if v_type == "uuv" else "",
        server_ws_url=config.get("server_ws_url", ""),
        rosbridge_url=config.get("rosbridge_url", ""),
        setpoint_hz=config.get("setpoint_hz", 5),
        f_5="selected" if frame == "5" else "",
        f_6="selected" if frame == "6" else "",
        f_11="selected" if frame == "11" else "",
        c_auto_arm="checked" if config.get("auto_arm_offboard", True) else "",
        c_discover="checked" if config.get("discover_mavros_topics", True) else "",
    )
    return web.Response(text=html, content_type="text/html")


async def handle_status_api(request: web.Request) -> web.Response:
    if time.time() - system_status["last_hb_time"] > 5.0 and system_status["last_hb_time"] > 0:
        system_status["ros_connected"] = False
        system_status["ros_status"] = "Heartbeat Lost"
    return web.json_response(system_status)


async def handle_save(request: web.Request) -> web.Response:
    data = await request.post()
    global config

    config["vehicle_id"] = str(data.get("vehicle_id", config["vehicle_id"])).strip()
    config["vehicle_type"] = str(data.get("vehicle_type", config.get("vehicle_type", "uav"))).strip()
    config["server_ws_url"] = str(data.get("server_ws_url", config["server_ws_url"])).strip()
    config["rosbridge_url"] = str(data.get("rosbridge_url", config["rosbridge_url"])).strip()
    config["setpoint_hz"] = float(data.get("setpoint_hz", config["setpoint_hz"]))
    config["global_setpoint_frame"] = int(data.get("global_setpoint_frame", config["global_setpoint_frame"]))
    config["auto_arm_offboard"] = data.get("auto_arm_offboard") == "true"
    config["discover_mavros_topics"] = data.get("discover_mavros_topics") == "true"

    save_config(config)
    reconnect_event.set()
    return web.HTTPFound(location="/")


async def start_web_server() -> None:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status_api)
    app.router.add_post("/save", handle_save)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(config.get("web_port", 8083))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[HTTP] Diagnostics interface running at http://0.0.0.0:{port}")


# --- MAVROS BRIDGE CLASS ---

class Bridge:
    def __init__(self) -> None:
        self.ros_ws: websockets.WebSocketClientProtocol | None = None
        self.vehicle_ws: websockets.WebSocketClientProtocol | None = None
        self.active_waypoint: dict[str, float] | None = None
        self.active_velocity: dict[str, float] | None = None
        self.last_heading: float | None = None
        self.topic_types = dict(DEFAULT_SUBSCRIPTIONS)

        self.current_pos: dict[str, float | None] = {"lat": None, "lon": None, "alt": None}
        self.streaming_task: asyncio.Task | None = None
        self.stream_stop_event = asyncio.Event()
        self.ship_states: dict[str,dict] = {}

    async def run_forever(self) -> None:
        vehicle_id = str(config.get("vehicle_id", "px4-uav"))
        server_url = str(config.get("server_ws_url", ""))
        rosbridge_url = str(config.get("rosbridge_url", ""))

        while not reconnect_event.is_set():
            try:
                system_status["ros_status"] = "Connecting..."
                system_status["ws_status"] = "Connecting..."

                async with websockets.connect(rosbridge_url, ping_interval=10, ping_timeout=10) as ros_ws:
                    system_status["ros_connected"] = True
                    system_status["ros_status"] = "Connected"
                    self.ros_ws = ros_ws

                    async with websockets.connect(f"{server_url.rstrip('/')}/{vehicle_id}", ping_interval=10, ping_timeout=10) as vehicle_ws:
                        system_status["ws_connected"] = True
                        system_status["ws_status"] = "Connected"
                        self.vehicle_ws = vehicle_ws

                        print(f"[BRIDGE] {vehicle_id} connected to ROSBridge ({rosbridge_url}) and GCS Server ({server_url})")
                        await self.setup_rosbridge()

                        run_task = asyncio.gather(
                            self.read_rosbridge(),
                            self.read_vehicle_commands(),
                            self.publish_setpoints(),
                            self.ship_state_listener_loop(),
                        )

                        # Wait until a connection drops OR settings are updated
                        reconnect_waiter = asyncio.create_task(reconnect_event.wait())
                        done, pending = await asyncio.wait([run_task, reconnect_waiter], return_when=asyncio.FIRST_COMPLETED)

                        for t in pending:
                            t.cancel()

                        if reconnect_event.is_set():
                            print("[BRIDGE] Settings changed via web UI. Restarting bridge connections...")
                            break

            except Exception as exc:
                print(f"[WARN] Bridge connection error: {exc}")
                system_status["ros_connected"] = False
                system_status["ros_status"] = "Disconnected"
                system_status["ws_connected"] = False
                system_status["ws_status"] = "Disconnected"
                self.ros_ws = None
                self.vehicle_ws = None
                await asyncio.sleep(2.0)

    async def setup_rosbridge(self) -> None:
        await self.ros_send({"op": "advertise", "topic": SETPOINT_TOPIC, "type": SETPOINT_TYPE})
        
        # 1. Set our active subscriptions strictly to the defaults
        self.topic_types = dict(DEFAULT_SUBSCRIPTIONS)

        # 2. Discover active topics purely for the Web UI display
        discovered = {}
        if config.get("discover_mavros_topics", True):
            discovered = await self.discover_mavros_topics()

        # Combine defaults and discovered for the dashboard, without subscribing to them
        display_topics = dict(self.topic_types)
        display_topics.update(discovered)
        system_status["discovered_topics"] = display_topics

        # 3. ONLY subscribe to our necessary default topics
        for topic, msg_type in sorted(self.topic_types.items()):
            await self.ros_send(
                {
                    "op": "subscribe",
                    "topic": topic,
                    "type": msg_type,
                    "throttle_rate": 100,  # 10Hz is safe for this limited list
                    "queue_length": 1,
                }
            )


    async def read_rosbridge(self) -> None:
        assert self.ros_ws
        async for raw in self.ros_ws:
            if reconnect_event.is_set():
                break
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if payload.get("op") != "publish":
                continue

            topic = str(payload.get("topic", ""))
            msg = payload.get("msg", {})
            msg_type = ros1_to_ros2_type(self.topic_types.get(topic, "unknown"))

            system_status["last_hb_time"] = time.time()
            self.update_telemetry_status(topic, msg)

            await self.forward(topic, msg_type, msg)
            for alias_topic, alias_type, alias_msg in self.canonical_aliases(topic, msg_type, msg):
                await self.forward(alias_topic, alias_type, alias_msg)

    def update_telemetry_status(self, topic: str, msg: dict[str, Any]) -> None:
        if not isinstance(msg, dict):
            return

        if topic == "/mavros/state":
            system_status["flight_mode"] = str(msg.get("mode", "UNKNOWN")).upper()
            system_status["armed"] = bool(msg.get("armed", False))

        elif topic in ("/mavros/gpsstatus/gps1/raw", "/mavros/global_position/global"):
            fix_type = int(msg.get("fix_type", msg.get("status", {}).get("status", 0)))
            fix_map = {0: "No GPS", 1: "No Fix", 2: "2D Fix", 3: "3D Fix", 4: "DGPS", 5: "RTK Float", 6: "RTK Fixed"}
            system_status["gps_status"] = fix_map.get(fix_type, f"Fix {fix_type}")
            if "satellites_visible" in msg:
                system_status["satellites"] = int(msg.get("satellites_visible", 0))

    async def read_vehicle_commands(self) -> None:
        assert self.vehicle_ws
        vehicle_id = str(config.get("vehicle_id", "px4-uav"))
        vehicle_type = str(config.get("vehicle_type", "uav"))

        async for raw in self.vehicle_ws:
            if reconnect_event.is_set():
                break
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            command = payload.get("command", payload)
            if not isinstance(command, dict):
                continue

            cmd_type = command.get("type")

            if cmd_type not in ("rtcm_data",):
                self.stream_stop_event.set()
                if self.streaming_task:
                    self.streaming_task.cancel()
                    self.streaming_task = None
                self.stream_stop_event.clear()

            if cmd_type == "waypoint":
                await self.handle_waypoint(command)
            elif cmd_type == "rtb_follow":
                await self.handle_rtb_follow(command)
            elif cmd_type == "land_on_boat_step":
                await self.handle_land_on_boat_step(command)
            elif cmd_type == "search_grid":
                self.streaming_task = asyncio.create_task(self.handle_search_grid(command))
            elif cmd_type == "mob":
                self.streaming_task = asyncio.create_task(self.handle_mob(command))
            elif cmd_type == "rtb":
                await self.call_service("/mavros/set_mode", "mavros_msgs/SetMode", {"base_mode": 0, "custom_mode": "AUTO.RTL"})
            elif cmd_type == "mission_plan":
                await self.handle_mission_plan(command)
            elif cmd_type == "absolute_trajectory":
                self.streaming_task = asyncio.create_task(self.handle_absolute_trajectory(command))
            elif cmd_type == "ship_relative_trajectory":
                self.streaming_task = asyncio.create_task(self.handle_ship_relative_trajectory(command))
            elif cmd_type == "set_mode":
                await self.handle_set_mode(command)
            elif cmd_type == "arm":
                await self.call_service("/mavros/cmd/arming", "mavros_msgs/CommandBool", {"value": True})
            elif cmd_type == "disarm":
                await self.call_service("/mavros/cmd/arming", "mavros_msgs/CommandBool", {"value": False})
            elif cmd_type == "takeoff" and vehicle_type not in ("usv", "ugv"):
                altitude_m = float(command.get("altitude_m") or 15.0)
                await self.call_service("/mavros/cmd/arming", "mavros_msgs/CommandBool", {"value": True})
                await self.call_service(
                    "/mavros/cmd/takeoff",
                    "mavros_msgs/CommandTOL",
                    {"min_pitch": 0.0, "yaw": 0.0, "latitude": 0.0, "longitude": 0.0, "altitude": altitude_m},
                )

    async def handle_waypoint(self, command: dict[str, Any]) -> None:
        target = command.get("target") or {}
        self.active_velocity = None
        self.auto_heading = True
        self.active_waypoint = {
            "latitude": float(target["latitude"]),
            "longitude": float(target["longitude"]),
            "altitude": float(target.get("altitude", 45.0)),
        }
        await self.publish_setpoint_once()
        if config.get("auto_arm_offboard", True):
            asyncio.create_task(self.enter_offboard_after_setpoints())

    async def handle_rtb_follow(self, command: dict[str, Any]) -> None:
        target = command.get("target") or {}
        self.auto_heading = False
        self.active_waypoint = {
            "latitude": float(target["latitude"]),
            "longitude": float(target["longitude"]),
            "altitude": float(target.get("altitude", 45.0)),
        }
        self.active_velocity = {
            "x": float(command.get("velocity_east_ms") or 0.0),
            "y": float(command.get("velocity_north_ms") or 0.0),
            "z": 0.0,
        }
        self.last_heading = float(command.get("heading") or 0.0)
        await self.publish_setpoint_once()
        if config.get("auto_arm_offboard", True):
            asyncio.create_task(self.enter_offboard_after_setpoints())

    async def handle_land_on_boat_step(self, command: dict[str, Any]) -> None:
        target = command.get("target") or {}
        self.auto_heading = False
        self.active_waypoint = {
            "latitude": float(target["latitude"]),
            "longitude": float(target["longitude"]),
            "altitude": float(target.get("altitude", 0.0)),
        }
        self.active_velocity = {
            "x": float(command.get("velocity_east_ms") or 0.0),
            "y": float(command.get("velocity_north_ms") or 0.0),
            "z": -float(command.get("sink_rate_ms") or 0.0),
        }
        self.last_heading = float(command.get("heading") or 0.0)
        await self.publish_setpoint_once()
        if config.get("auto_arm_offboard", True):
            asyncio.create_task(self.enter_offboard_after_setpoints())

    async def handle_search_grid(self, command: dict[str, Any]) -> None:
        lat = float(command.get("lat", 0))
        lon = float(command.get("lon", 0))
        grid_size_m = float(command.get("grid_size_m", 200))
        swath_m = float(command.get("swath_m", 20))
        altitude_m = float(command.get("altitude_m", 30))

        waypoints = behaviors_agnostic.calculate_search_grid_waypoints(lat, lon, grid_size_m, swath_m, altitude_m)
        await self.stream_waypoints(waypoints, arrival_radius=10.0)

    async def handle_mob(self, command: dict[str, Any]) -> None:
        track_points = command.get("track_points", [])
        corridor_half_width_m = float(command.get("corridor_half_width_m", 50.0))
        swath_m = float(command.get("swath_m", 20.0))
        altitude_m = float(command.get("altitude_m", 30.0))

        start_from_newest = False
        if self.current_pos["lat"] is not None and len(track_points) >= 2:
            dist_oldest = _distance_m(self.current_pos["lat"], self.current_pos["lon"], float(track_points[0][0]), float(track_points[0][1]))
            dist_newest = _distance_m(self.current_pos["lat"], self.current_pos["lon"], float(track_points[-1][0]), float(track_points[-1][1]))
            start_from_newest = dist_newest < dist_oldest

        waypoints = behaviors_agnostic.calculate_mob_waypoints(
            track_points, corridor_half_width_m, swath_m, altitude_m, start_from_newest=start_from_newest
        )
        await self.stream_waypoints(waypoints, arrival_radius=10.0)

    async def stream_waypoints(self, waypoints: list[tuple[float, float, float]], arrival_radius: float) -> None:
        if config.get("auto_arm_offboard", True):
            await self.call_service("/mavros/cmd/arming", "mavros_msgs/CommandBool", {"value": True})

        v_type = str(config.get("vehicle_type", "uav"))

        for wp_lat, wp_lon, wp_alt in waypoints:
            if self.stream_stop_event.is_set():
                break

            self.active_velocity = None
            self.active_waypoint = {"latitude": wp_lat, "longitude": wp_lon, "altitude": wp_alt}

            if config.get("auto_arm_offboard", True):
                asyncio.create_task(self.enter_offboard_after_setpoints())

            while not self.stream_stop_event.is_set():
                if self.current_pos["lat"] is not None and self.current_pos["lon"] is not None:
                    dist = _distance_m(self.current_pos["lat"], self.current_pos["lon"], wp_lat, wp_lon)
                    if v_type in ["usv", "ugv"] or self.current_pos["alt"] is None:
                        if dist <= arrival_radius:
                            break
                    else:
                        alt_err = abs(self.current_pos["alt"] - wp_alt)
                        if dist <= arrival_radius and alt_err <= max(3.0, arrival_radius * 0.5):
                            break
                await asyncio.sleep(0.5)

    async def handle_mission_plan(self, command: dict[str, Any]) -> None:
        waypoints = command.get("waypoints") or []
        item_type_to_cmd = {"waypoint": 16, "takeoff": 22, "loiter_time": 19, "land": 21, "rtl": 20, "do_jump": 177}
        mav_waypoints: list[dict[str, Any]] = []

        for index, waypoint in enumerate(waypoints):
            if not isinstance(waypoint, dict):
                continue
            lat, lon = waypoint.get("latitude"), waypoint.get("longitude")
            if lat is None or lon is None:
                continue
            item_type = str(waypoint.get("item_type") or "waypoint").lower()
            command_id = int(waypoint.get("command_id") or item_type_to_cmd.get(item_type, 16))
            default_p1 = float(waypoint.get("hold_time_s", 0.0))
            default_p2 = float(waypoint.get("acceptance_radius_m", 8.0))
            default_p3 = 0.0
            default_p4 = float(waypoint.get("yaw_deg", 0.0) or 0.0)

            mav_waypoints.append(
                {
                    "frame": 3,
                    "command": command_id,
                    "is_current": index == 0,
                    "autocontinue": True,
                    "param1": float(waypoint.get("param1", default_p1)),
                    "param2": float(waypoint.get("param2", default_p2)),
                    "param3": float(waypoint.get("param3", default_p3)),
                    "param4": float(waypoint.get("param4", default_p4)),
                    "x_lat": float(lat),
                    "y_long": float(lon),
                    "z_alt": float(waypoint.get("altitude", 45.0)),
                }
            )

        if not mav_waypoints:
            return

        await self.call_service("/mavros/mission/clear", "mavros_msgs/WaypointClear", {})
        await self.call_service(
            "/mavros/mission/push",
            "mavros_msgs/WaypointPush",
            {"start_index": 0, "waypoints": mav_waypoints},
        )

        if bool(command.get("auto_arm_start", True)):
            await self.call_service("/mavros/cmd/arming", "mavros_msgs/CommandBool", {"value": True})
            await self.call_service("/mavros/set_mode", "mavros_msgs/SetMode", {"base_mode": 0, "custom_mode": "AUTO.MISSION"})

    async def handle_set_mode(self, command: dict[str, Any]) -> None:
        mode = command.get("mode")
        if not mode:
            print("[WARN] set_mode missing mode field")
            return
        mode_str = str(mode).upper()
        if mode_str in ["RTL", "LAND", "LOITER", "MISSION", "TAKEOFF"]:
            custom_mode = f"AUTO.{mode_str}"
        elif mode_str in ["OFFBOARD", "STABILIZED", "ALTITUDE_CONTROL", "POSITION_CONTROL", "MANUAL"]:
            custom_mode = mode_str
        else:
            custom_mode = f"AUTO.{mode_str}"

        await self.call_service("/mavros/set_mode", "mavros_msgs/SetMode", {"base_mode": 0, "custom_mode": custom_mode})
        print(f"[PX4] Set mode to {custom_mode}")

    async def enter_offboard_after_setpoints(self) -> None:
        await asyncio.sleep(1.2)
        await self.call_service("/mavros/set_mode", "mavros_msgs/SetMode", {"base_mode": 0, "custom_mode": "OFFBOARD"})

    async def publish_setpoints(self) -> None:
        hz = float(config.get("setpoint_hz", 5))
        while not reconnect_event.is_set():
            await self.publish_setpoint_once()
            await asyncio.sleep(1.0 / max(1.0, hz))

    async def publish_setpoint_once(self) -> None:
        if not self.active_waypoint:
            return

        yaw_ned_deg = self.last_heading or 0.0
        feedforward_velocity = getattr(self, "active_velocity", None)
        clamped_waypoint = dict(self.active_waypoint)

        if self.current_pos["lat"] is not None:
            dist = _distance_m(
                self.current_pos["lat"], self.current_pos["lon"], 
                self.active_waypoint["latitude"], self.active_waypoint["longitude"]
            )
            
            if getattr(self, "auto_heading", True) and dist > 1.0:
                yaw_ned_deg = behaviors_agnostic._bearing_between(
                    self.current_pos["lat"], self.current_pos["lon"],
                    self.active_waypoint["latitude"], self.active_waypoint["longitude"]
                )
                self.last_heading = yaw_ned_deg

            # CARROT CHASE: Limit position setpoint to 50m to prevent PX4 step-error stalling
            if dist > 50.0 and feedforward_velocity is None:
                clamped_lat, clamped_lon = behaviors_agnostic._offset_position(
                    self.current_pos["lat"], self.current_pos["lon"],
                    yaw_ned_deg, 50.0
                )
                clamped_waypoint["latitude"] = clamped_lat
                clamped_waypoint["longitude"] = clamped_lon

        yaw_enu_deg = (90.0 - yaw_ned_deg) % 360.0
        ignore_yaw = getattr(self, "ignore_yaw_flag", False)

        await self.ros_send(
            {
                "op": "publish",
                "topic": SETPOINT_TOPIC,
                "type": SETPOINT_TYPE,
                "msg": global_position_target(
                    clamped_waypoint, 
                    yaw_enu_deg, 
                    feedforward_velocity,
                    ignore_yaw
                ),
            }
        )

    async def handle_absolute_trajectory(self, command: dict[str, Any]) -> None:
        waypoints = command.get("waypoints", [])
        arrival_radius = float(command.get("arrival_radius_m", 10.0))
        if not waypoints:
            return
            
        formatted = []
        for wp in waypoints:
            lat, lon, alt = wp.get("latitude"), wp.get("longitude"), wp.get("altitude", 45.0)
            if lat is not None and lon is not None:
                formatted.append((float(lat), float(lon), float(alt)))
        
        self.auto_heading = True
        await self.stream_waypoints(formatted, arrival_radius)

    async def handle_ship_relative_trajectory(self, command: dict[str, Any]) -> None:
        ship_id = command.get("ship_vehicle_id")
        local_waypoints = command.get("local_waypoints", [])
        arrival_radius = float(command.get("arrival_radius_m", 6.0))
        update_hz = float(command.get("update_hz", 10.0))
        
        if not ship_id or not local_waypoints:
            return

        if config.get("auto_arm_offboard", True):
            await self.call_service("/mavros/cmd/arming", "mavros_msgs/CommandBool", {"value": True})

        for index, waypoint in enumerate(local_waypoints):
            if self.stream_stop_event.is_set():
                break
                
            self.auto_heading = False 
            if config.get("auto_arm_offboard", True):
                asyncio.create_task(self.enter_offboard_after_setpoints())

            while not self.stream_stop_event.is_set():
                ship = self.ship_states.get(ship_id)
                if not ship or (time.time() - ship["stamp"]) > 2.0:
                    await asyncio.sleep(1.0 / update_hz)
                    continue
                    
                ship_heading = ship["heading_deg"]
                target_lat, target_lon, target_alt = behaviors_agnostic._relative_waypoint_to_global(
                    ship["lat"], ship["lon"], ship_heading, ship["alt"], waypoint
                )
                
                if config.get("vehicle_type", "uav") in ["usv", "ugv"]:
                    target_alt = 0.0
                    
                yaw_deg = waypoint.get("yaw_deg")
                if yaw_deg is not None:
                    self.last_heading = behaviors_agnostic._relative_yaw_to_global(ship_heading, float(yaw_deg))
                    self.ignore_yaw_flag = False
                else:
                    self.ignore_yaw_flag = True
                    
                self.active_waypoint = {"latitude": target_lat, "longitude": target_lon, "altitude": target_alt}
                
                # TRANSLATION FIX: MAVROS expects ENU (X=East, Y=North, Z=Up)
                self.active_velocity = None
                                
                if self.current_pos["lat"] is not None:
                    dist = _distance_m(self.current_pos["lat"], self.current_pos["lon"], target_lat, target_lon)
                    alt_condition = True if config.get("vehicle_type", "uav") in ["usv", "ugv"] else abs((self.current_pos["alt"] or 0) - target_alt) <= max(2.0, arrival_radius * 0.5)
                    if dist <= arrival_radius and alt_condition:
                        break
                        
                await asyncio.sleep(1.0 / update_hz)

    async def ship_state_listener_loop(self) -> None:
        server_url = str(config.get("server_ws_url", ""))
        base = server_url.rstrip("/")
        marker = "/ws/vehicle"
        ui_ws_url = f"{base.split(marker, 1)[0]}/ws/ui" if marker in base else base
        
        while not reconnect_event.is_set():
            try:
                async with websockets.connect(ui_ws_url, ping_interval=10, ping_timeout=10) as ws:
                    async for raw_message in ws:
                        if reconnect_event.is_set():
                            break
                        try:
                            msg = json.loads(raw_message)
                            op = msg.get("op")
                            if op == "snapshot":
                                for v in msg.get("vehicles", []):
                                    if v.get("vehicle_type") == "yp":
                                        self.update_ship_state(v)
                            elif op == "vehicle_update":
                                v = msg.get("vehicle") or {}
                                if v.get("vehicle_type") == "yp":
                                        self.update_ship_state(v)
                        except json.JSONDecodeError:
                            pass
            except Exception:
                await asyncio.sleep(2.0)

    def update_ship_state(self, vehicle: dict[str, Any]) -> None:
        vid = vehicle.get("vehicle_id")
        pos = vehicle.get("position") or {}
        lat = pos.get("latitude")
        lon = pos.get("longitude")
        if lat is None or lon is None or not vid:
            return
            
        stamp = float(vehicle.get("last_seen") or time.time())
        prev = self.ship_states.get(vid, {})
        prev_lat, prev_lon, prev_stamp = prev.get("lat"), prev.get("lon"), prev.get("stamp", 0.0)
        
        vn_ms, ve_ms = prev.get("vn_ms", 0.0), prev.get("ve_ms", 0.0)
        
        if prev_lat is not None and prev_lon is not None and stamp > prev_stamp:
            north_m, east_m = behaviors_agnostic._north_east_delta_m(prev_lat, prev_lon, lat, lon)
            dt = stamp - prev_stamp
            if dt > 0:
                vn_ms = north_m / dt
                ve_ms = east_m / dt
                
        self.ship_states[vid] = {
            "lat": lat,
            "lon": lon,
            "alt": pos.get("altitude", 0.0),
            "heading_deg": float(vehicle.get("heading") or prev.get("heading_deg", 0.0)) % 360.0,
            "vn_ms": vn_ms,
            "ve_ms": ve_ms,
            "stamp": stamp
        }

    async def call_service(self, service: str, service_type: str, args: dict[str, Any]) -> None:
        vehicle_id = str(config.get("vehicle_id", "px4-uav"))
        await self.ros_send(
            {
                "op": "call_service",
                "service": service,
                "type": service_type,
                "args": args,
                "id": f"{vehicle_id}-{service}-{time.time()}",
            }
        )

    async def discover_mavros_topics(self) -> dict[str, str]:
        vehicle_id = str(config.get("vehicle_id", "px4-uav"))
        try:
            values = await self.call_rosapi("/rosapi/topics", {})
            topics = values.get("topics", [])
            discovered: dict[str, str] = {}
            for topic in topics:
                if not isinstance(topic, str) or not topic.startswith("/mavros/"):
                    continue
                type_values = await self.call_rosapi("/rosapi/topic_type", {"topic": topic})
                msg_type = type_values.get("type")
                if isinstance(msg_type, str) and msg_type:
                    discovered[topic] = msg_type
            if discovered:
                print(f"[BRIDGE] {vehicle_id} discovered {len(discovered)} active MAVROS topics")
            return discovered
        except Exception as exc:
            print(f"[WARN] {vehicle_id} MAVROS topic discovery skipped: {exc}")
            return {}

    async def call_rosapi(self, service: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self.ros_ws:
            return {}
        vehicle_id = str(config.get("vehicle_id", "px4-uav"))
        request_id = f"{vehicle_id}-{service}-{time.time()}"
        await self.ros_ws.send(json.dumps({"op": "call_service", "service": service, "args": args, "id": request_id}))
        deadline = time.time() + 3.0
        while time.time() < deadline:
            raw = await asyncio.wait_for(self.ros_ws.recv(), timeout=max(0.1, deadline - time.time()))
            payload = json.loads(raw)
            if payload.get("op") == "service_response" and payload.get("id") == request_id:
                values = payload.get("values", {})
                return values if isinstance(values, dict) else {}
        return {}

    async def forward(self, topic: str, msg_type: str, msg: Any) -> None:
        if not self.vehicle_ws:
            return
        vehicle_id = str(config.get("vehicle_id", "px4-uav"))
        vehicle_type = str(config.get("vehicle_type", "uav"))

        await self.vehicle_ws.send(
            json.dumps(
                {
                    "vehicle_id": vehicle_id,
                    "vehicle_type": vehicle_type,
                    "topic": topic_for_vehicle(topic, vehicle_id),
                    "type": msg_type,
                    "stamp": time.time(),
                    "msg": msg,
                }
            )
        )

    def canonical_aliases(self, topic: str, msg_type: str, msg: Any) -> list[tuple[str, str, dict[str, Any]]]:
        if not isinstance(msg, dict):
            return []

        # 1. Track relative altitude (AGL) so ground level is 0.0m
        if topic == "/mavros/global_position/rel_alt":
            self.current_pos["alt"] = msg.get("data")
            return []

        if topic == "/mavros/global_position/global":
            self.current_pos["lat"] = msg.get("latitude")
            self.current_pos["lon"] = msg.get("longitude")
            
            # Inject the relative altitude into the outgoing NavSatFix message 
            # so the GCS UI matches the relative altitude commanded in waypoints.
            if self.current_pos["alt"] is not None:
                msg["altitude"] = self.current_pos["alt"]
                
            return [("navsatfix", "sensor_msgs/msg/NavSatFix", msg)]

        if topic == "/mavros/local_position/pose":
            pose = msg.get("pose")
            if isinstance(pose, dict):
                return [("pose", "geometry_msgs/msg/Pose", pose)]

        if topic == "/mavros/battery":
            return [("battery", "sensor_msgs/msg/BatteryState", msg)]

        if topic == "/mavros/global_position/compass_hdg":
            heading = msg.get("data")
            if isinstance(heading, (int, float)):
                self.last_heading = float(heading) % 360
                return [("heading", "yp_ground_station/msg/Heading", {"heading": self.last_heading})]
                
        return []

    async def ros_send(self, payload: dict[str, Any]) -> None:
        if not self.ros_ws:
            return
        await self.ros_ws.send(json.dumps(payload))


# --- HELPER FUNCTIONS ---

def ros1_to_ros2_type(msg_type: str) -> str:
    if "/" not in msg_type or "/msg/" in msg_type:
        return msg_type
    package, name = msg_type.split("/", 1)
    return f"{package}/msg/{name}"


def topic_for_vehicle(topic: str, vehicle_id: str) -> str:
    clean = topic.strip("/")
    if clean.startswith("mavros/"):
        return f"/vehicles/{vehicle_id}/{clean}"
    return f"/vehicles/{vehicle_id}/{clean or 'unknown'}"


def global_position_target(
    target: dict[str, float], 
    yaw_enu_deg: float, 
    velocity: dict[str, float] | None = None,
    ignore_yaw: bool = False
) -> dict[str, Any]:
    if velocity is not None:
        ignore_position = 3   # Ignore X/Y (1 | 2), keep Z altitude active (0)
        ignore_velocity = 0   # Enable Velocity (0)
    else:
        ignore_position = 0   # Enable X/Y/Z Position (0)
        ignore_velocity = 56  # Ignore Vx/Vy/Vz (8 | 16 | 32)
        
    ignore_accel = 448        # Ignore Ax/Ay/Az (64 | 128 | 256)
    ignore_yaw_bit = 1024 if ignore_yaw else 0  
    ignore_yaw_rate = 2048
    
    type_mask = ignore_position | ignore_velocity | ignore_accel | ignore_yaw_bit | ignore_yaw_rate
    yaw_rad = math.radians(yaw_enu_deg)
    frame = int(config.get("global_setpoint_frame", 6))

    return {
        "header": ros_header("map"),
        "coordinate_frame": frame,
        "type_mask": type_mask,
        "latitude": target["latitude"],
        "longitude": target["longitude"],
        "altitude": target["altitude"],
        "velocity": velocity or {"x": 0.0, "y": 0.0, "z": 0.0},
        "acceleration_or_force": {"x": 0.0, "y": 0.0, "z": 0.0},
        "yaw": yaw_rad,
        "yaw_rate": 0.0,
    }


def ros_header(frame_id: str) -> dict[str, Any]:
    stamp = time.time()
    sec = int(stamp)
    return {"stamp": {"secs": sec, "nsecs": int((stamp - sec) * 1_000_000_000)}, "frame_id": frame_id}


# --- MAIN ENTRYPOINT ---

async def main() -> None:
    global config
    config = load_config()

    await start_web_server()

    while True:
        reconnect_event.clear()
        bridge = Bridge()
        bridge_task = asyncio.create_task(bridge.run_forever())

        await reconnect_event.wait()

        print("[HTTP] Settings updated via web UI! Terminating and restarting bridge tasks...")
        bridge_task.cancel()
        await asyncio.gather(bridge_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down MAVROS bridge cleanly")