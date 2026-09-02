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

from aiohttp import web
from pymavlink import mavutil
import websockets

import sar_missions

CONFIG_PATH = Path("config.json")

def _resolve_webrtc_ip() -> str:
    configured_ip = os.getenv("WEBRTC_IP")
    if configured_ip:
        return configured_ip
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            detected_ip = sock.getsockname()[0]
            if detected_ip and not detected_ip.startswith("127."):
                return detected_ip
    except OSError:
        pass
    try:
        detected_ip = socket.gethostbyname(socket.gethostname())
        if detected_ip and not detected_ip.startswith("127."):
            return detected_ip
    except OSError:
        pass
    return "127.0.0.1"

# --- DEFAULT CONFIGURATION ---
DEFAULT_CONFIG = {
    "server_ws_url": os.getenv("SERVER_WS_URL", "ws://192.168.0.174:8000/ws/vehicle"),
    "vehicle_id": os.getenv("VEHICLE_ID", "quadrotorYP"),
    "mavlink_url": os.getenv("MAVLINK_URL", "/dev/serial0"),
    "mavlink_baud": int(os.getenv("MAVLINK_BAUD", "921600")),
    "send_hz": float(os.getenv("SEND_HZ", "5")),
    "web_port": 8080,
}

config = {}
reconnect_event = asyncio.Event()

system_status = {
    "cube_connected": False,
    "cube_status": "Connecting...",
    "ws_connected": False,
    "ws_status": "Connecting...",
    "flight_mode": "UNKNOWN",
    "gps_status": "No Fix",
    "satellites": 0,
    "last_hb_time": 0,
}

# SAR & Global Variables
VEHICLE_TYPE = os.getenv("VEHICLE_TYPE", "uav")
WEBRTC_IP = _resolve_webrtc_ip()
SAR_TAKEOFF_ALT_M = float(os.getenv("SAR_TAKEOFF_ALT_M", "30.0"))
SAR_CLIMB_SPEED_MS = float(os.getenv("SAR_CLIMB_SPEED_MS", "8.0"))
SAR_INCLUDE_TAKEOFF = os.getenv("SAR_INCLUDE_TAKEOFF", "true").lower() != "false"
SAR_STREAMING_MODE = os.getenv("SAR_STREAMING_MODE", "true").lower() != "false"
SAR_ARRIVAL_RADIUS_M = float(os.getenv("SAR_ARRIVAL_RADIUS_M", "10.0"))

_sar_mission_lock = threading.Lock()
_sar_stop_event = threading.Event()
_sar_telemetry_lock = threading.Lock()
_sar_latest_nav = {"lat": None, "lon": None, "alt": None, "heading": None, "stamp": 0.0}

SHIP_STATE_TIMEOUT_S = float(os.getenv("SHIP_STATE_TIMEOUT_S", "2.0"))
SHIP_RELATIVE_DEFAULT_UPDATE_HZ = float(os.getenv("SHIP_RELATIVE_UPDATE_HZ", "10.0"))
SHIP_RELATIVE_DEFAULT_ARRIVAL_RADIUS_M = float(os.getenv("SHIP_RELATIVE_ARRIVAL_RADIUS_M", "6.0"))
EARTH_RADIUS_M = 6_378_137.0

_vehicle_state_lock = threading.Lock()
_vehicle_state = {"lat": None, "lon": None, "alt": None, "heading_deg": None, "stamp": 0.0}
_ship_state_lock = threading.Lock()
_ship_state = {"vehicle_id": None, "lat": None, "lon": None, "alt": None, "heading_deg": None, "vn_ms": 0.0, "ve_ms": 0.0, "stamp": 0.0}
_ship_relative_thread: threading.Thread | None = None
_ship_relative_stop_event = threading.Event()

# --- CONFIG MANAGEMENT & WEB SERVER ---

def load_config() -> dict:
    if CONFIG_PATH.is_file():
        try:
            with open(CONFIG_PATH, "r") as f:
                saved = json.load(f)
                return {**DEFAULT_CONFIG, **saved}
        except Exception as e:
            print(f"Error loading config.json, using defaults: {e}")
    return DEFAULT_CONFIG.copy()

def save_config(new_config: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(new_config, f, indent=2)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Arducopter Bridge - Telemetry Diagnostics & Config</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; padding: 20px; max-width: 550px; margin: 0 auto; }}
        h2 {{ color: #38bdf8; border-bottom: 2px solid #334155; padding-bottom: 10px; margin-bottom: 15px; }}
        .diag-card {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 15px; margin-bottom: 25px; }}
        .diag-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 10px; }}
        .diag-item {{ background: #0f172a; padding: 10px; border-radius: 6px; border: 1px solid #1e293b; }}
        .diag-label {{ font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px; }}
        .diag-value {{ font-size: 1.05rem; font-weight: 600; margin-top: 4px; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.85rem; font-weight: 600; }}
        .badge-online {{ background: #065f46; color: #34d399; }}
        .badge-offline {{ background: #881337; color: #fda4af; }}
        label {{ display: block; margin-top: 15px; font-weight: 600; font-size: 0.9rem; color: #94a3b8; }}
        input, select {{ width: 100%; padding: 10px; margin-top: 5px; border-radius: 6px; border: 1px solid #475569; background: #1e293b; color: white; box-sizing: border-box; font-size: 1rem; }}
        button {{ width: 100%; margin-top: 25px; padding: 12px; background: #2563eb; color: white; border: none; border-radius: 6px; font-size: 1rem; font-weight: bold; cursor: pointer; }}
        button:hover {{ background: #1d4ed8; }}
    </style>
</head>
<body>
    <h2>System Diagnostics</h2>
    <div class="diag-card">
        <div class="diag-grid">
            <div class="diag-item">
                <div class="diag-label">Cube Controller</div>
                <div class="diag-value"><span id="cube_status" class="badge badge-offline">Offline</span></div>
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

    <h2>Configuration</h2>
    <form method="POST" action="/save">
        <label>GCS Server WebSocket URL</label>
        <input type="text" name="server_ws_url" value="{server_ws_url}" required>

        <label>Vehicle ID</label>
        <input type="text" name="vehicle_id" value="{vehicle_id}" required>

        <label>MAVLink URL (Connection)</label>
        <select id="mavlink_url_select" name="mavlink_url_select" onchange="toggleCustomUrl()">
            <option value="/dev/serial0" {s_serial0}>/dev/serial0 (Pi GPIO)</option>
            <option value="/dev/ttyACM0" {s_acm0}>/dev/ttyACM0 (USB Flight Controller)</option>
            <option value="/dev/ttyUSB0" {s_usb0}>/dev/ttyUSB0 (USB Telemetry Radio)</option>
            <option value="custom" {s_custom}>Custom IP / Other...</option>
        </select>
        <input type="text" id="mavlink_url_custom" name="mavlink_url_custom" value="{mavlink_url_custom}" style="display: {custom_display}; margin-top: 8px;" placeholder="e.g. tcp:192.168.1.50:5760">

        <label>Baud Rate</label>
        <select name="mavlink_baud">
            <option value="921600" {b921600}>921600 (PiConnect Default)</option>
            <option value="115200" {b115200}>115200 (USB Default)</option>
            <option value="57600" {b57600}>57600 (Telemetry Radio)</option>
            <option value="9600" {b9600}>9600</option>
        </select>

        <label>Update Rate (Hz)</label>
        <input type="number" step="0.1" name="send_hz" value="{send_hz}" required>

        <button type="submit">Save & Restart Telemetry Stream</button>
    </form>

    <script>
        function toggleCustomUrl() {{
            const select = document.getElementById('mavlink_url_select');
            const customInput = document.getElementById('mavlink_url_custom');
            if (select.value === 'custom') {{
                customInput.style.display = 'block';
                customInput.required = true;
            }} else {{
                customInput.style.display = 'none';
                customInput.required = false;
            }}
        }}
        window.addEventListener('DOMContentLoaded', toggleCustomUrl);

        async function fetchStatus() {{
            try {{
                const res = await fetch('/api/status');
                const data = await res.json();
                
                const cubeElem = document.getElementById('cube_status');
                cubeElem.innerText = data.cube_status;
                cubeElem.className = 'badge ' + (data.cube_connected ? 'badge-online' : 'badge-offline');

                const wsElem = document.getElementById('ws_status');
                wsElem.innerText = data.ws_status;
                wsElem.className = 'badge ' + (data.ws_connected ? 'badge-online' : 'badge-offline');

                document.getElementById('flight_mode').innerText = data.flight_mode;
                
                const gpsText = data.gps_status + (data.satellites > 0 ? ` (${{data.satellites}} Sats)` : '');
                document.getElementById('gps_status').innerText = gpsText;
            }} catch (e) {{ console.error("Failed fetching status", e); }}
        }}
        setInterval(fetchStatus, 1000);
        fetchStatus();
    </script>
</body>
</html>
"""

async def handle_index(request):
    url = config["mavlink_url"]
    known_ports = ["/dev/serial0", "/dev/ttyACM0", "/dev/ttyUSB0"]
    is_custom = url not in known_ports

    html = HTML_TEMPLATE.format(
        server_ws_url=config["server_ws_url"],
        vehicle_id=config["vehicle_id"],
        s_serial0="selected" if url == "/dev/serial0" else "",
        s_acm0="selected" if url == "/dev/ttyACM0" else "",
        s_usb0="selected" if url == "/dev/ttyUSB0" else "",
        s_custom="selected" if is_custom else "",
        mavlink_url_custom=url if is_custom else "",
        custom_display="block" if is_custom else "none",
        send_hz=config["send_hz"],
        b921600="selected" if config["mavlink_baud"] == 921600 else "",
        b115200="selected" if config["mavlink_baud"] == 115200 else "",
        b57600="selected" if config["mavlink_baud"] == 57600 else "",
        b9600="selected" if config["mavlink_baud"] == 9600 else "",
    )
    return web.Response(text=html, content_type="text/html")

async def handle_status_api(request):
    if time.time() - system_status["last_hb_time"] > 4.0:
        system_status["cube_connected"] = False
        system_status["cube_status"] = "Heartbeat Lost"
    return web.json_response(system_status)

async def handle_save(request):
    data = await request.post()
    global config
    
    url_select = data.get("mavlink_url_select")
    if url_select == "custom":
        config["mavlink_url"] = data.get("mavlink_url_custom", "").strip()
    elif url_select:
        config["mavlink_url"] = url_select.strip()
        
    config["server_ws_url"] = data.get("server_ws_url", config["server_ws_url"]).strip()
    config["vehicle_id"] = data.get("vehicle_id", config["vehicle_id"]).strip()
    config["mavlink_baud"] = int(data.get("mavlink_baud", config["mavlink_baud"]))
    config["send_hz"] = float(data.get("send_hz", config["send_hz"]))

    save_config(config)
    reconnect_event.set()
    return web.HTTPFound(location="/")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status_api)
    app.router.add_post("/save", handle_save)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.get("web_port", 8080))
    await site.start()
    print(f"Web interface running at http://0.0.0.0:{config.get('web_port', 8080)}")

# --- HELPER FUNCTIONS ---

def get_gps_fix_label(fix_type: int) -> str:
    fix_map = {0: "No GPS", 1: "No Fix", 2: "2D Fix", 3: "3D Fix", 4: "DGPS", 5: "RTK Float", 6: "RTK Fixed"}
    return fix_map.get(fix_type, f"Fix {fix_type}")

def create_navsatfix_message(vehicle_id: str, lat: float, lon: float, alt: float, heading: float | None = None) -> dict:
    now = time.time()
    sec = int(now)
    nanosec = int((now - sec) * 1e9)
    payload = {
        "vehicle_id": vehicle_id,
        "vehicle_type": VEHICLE_TYPE,
        "topic": f"/vehicles/{vehicle_id}/navsatfix",
        "type": "sensor_msgs/msg/NavSatFix",
        "stamp": now,
        "msg": {
            "header": {"stamp": {"sec": sec, "nanosec": nanosec}, "frame_id": "map"},
            "status": {"status": 0, "service": 1},
            "latitude": lat, "longitude": lon, "altitude": alt,
            "position_covariance": [0.0] * 9, "position_covariance_type": 0,
        },
    }
    if heading is not None:
        payload["msg"]["heading"] = heading
    return payload

def create_video_stream_message(vehicle_id: str, webrtc_ip: str) -> dict:
    return {
        "op": "video_stream_update",
        "video": {
            "vehicle_id": vehicle_id,
            "enabled": True,
            "streams": [{"label": "Primary WebRTC", "url": f"http://{webrtc_ip}:8889/cam/whep"}]
        }
    }

def _ui_ws_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    marker = "/ws/vehicle"
    if marker in base:
        return f"{base.split(marker, 1)[0]}/ws/ui"
    return base

def _destination_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    bearing_rad = math.radians(bearing_deg)
    angular = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(math.sin(lat_rad) * math.cos(angular) + math.cos(lat_rad) * math.sin(angular) * math.cos(bearing_rad))
    lon2 = lon_rad + math.atan2(math.sin(bearing_rad) * math.sin(angular) * math.cos(lat_rad), math.cos(angular) - math.sin(lat_rad) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)

def _relative_waypoint_to_global(ship_lat: float, ship_lon: float, ship_heading: float, ship_alt: float, waypoint: dict) -> tuple[float, float, float]:
    local_x, local_y, local_z = float(waypoint.get("x", 0.0)), float(waypoint.get("y", 0.0)), float(waypoint.get("z", 0.0))
    distance_m = math.hypot(local_x, local_y)
    relative_bearing_deg = math.degrees(math.atan2(local_x, local_y))
    bearing_deg = (ship_heading + relative_bearing_deg + 360.0) % 360.0
    target_lat, target_lon = _destination_point(ship_lat, ship_lon, bearing_deg, distance_m)
    return target_lat, target_lon, ship_alt + local_z

def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
    a = math.sin((lat2_rad - lat1_rad) / 2.0) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(math.radians(lon2 - lon1) / 2.0) ** 2
    return EARTH_RADIUS_M * 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))

def _north_east_delta_m(lat_ref: float, lon_ref: float, lat: float, lon: float) -> tuple[float, float]:
    lat_avg = math.radians((lat_ref + lat) / 2.0)
    return math.radians(lat - lat_ref) * EARTH_RADIUS_M, math.radians(lon - lon_ref) * EARTH_RADIUS_M * math.cos(lat_avg)

def _update_vehicle_state(lat: float, lon: float, alt: float, heading: float | None) -> None:
    with _vehicle_state_lock:
        _vehicle_state.update({"lat": lat, "lon": lon, "alt": alt, "heading_deg": heading, "stamp": time.time()})

def _capture_sar_telemetry(msg) -> None:
    heading_raw = getattr(msg, "hdg", None)
    with _sar_telemetry_lock:
        _sar_latest_nav.update({
            "lat": msg.lat / 1e7, "lon": msg.lon / 1e7, "alt": msg.relative_alt / 1000.0,
            "heading": (heading_raw / 100.0) if heading_raw is not None and heading_raw != 65535 else None,
            "stamp": time.time()
        })

def _snapshot_sar_telemetry(max_age_s: float = 2.0) -> tuple[float, float, float, float | None] | None:
    with _sar_telemetry_lock:
        lat, lon, alt, heading, stamp = _sar_latest_nav.get("lat"), _sar_latest_nav.get("lon"), _sar_latest_nav.get("alt"), _sar_latest_nav.get("heading"), float(_sar_latest_nav.get("stamp") or 0.0)
    if None in (lat, lon, alt) or (time.time() - stamp) > max_age_s:
        return None
    return float(lat), float(lon), float(alt), (float(heading) if heading is not None else None)

def _snapshot_vehicle_state() -> dict:
    with _vehicle_state_lock:
        return dict(_vehicle_state)

def _update_ship_state(vehicle: dict) -> None:
    pos = vehicle.get("position") or {}
    lat, lon = pos.get("latitude"), pos.get("longitude")
    if lat is None or lon is None: return
    stamp = float(vehicle.get("last_seen") or time.time())
    with _ship_state_lock:
        prev_lat, prev_lon, prev_stamp = _ship_state.get("lat"), _ship_state.get("lon"), float(_ship_state.get("stamp") or 0.0)
        vn_ms, ve_ms = float(_ship_state.get("vn_ms") or 0.0), float(_ship_state.get("ve_ms") or 0.0)
        if prev_lat is not None and prev_lon is not None and stamp > prev_stamp:
            north_m, east_m = _north_east_delta_m(float(prev_lat), float(prev_lon), float(lat), float(lon))
            dt = stamp - prev_stamp
            if dt > 0: vn_ms, ve_ms = north_m / dt, east_m / dt
        _ship_state.update({"vehicle_id": vehicle.get("vehicle_id"), "lat": float(lat), "lon": float(lon), "alt": float(pos.get("altitude", 0.0)), "heading_deg": float(vehicle.get("heading")) % 360.0 if vehicle.get("heading") is not None else _ship_state.get("heading_deg"), "vn_ms": vn_ms, "ve_ms": ve_ms, "stamp": stamp})

def _snapshot_ship_state(expected_vehicle_id: str | None = None) -> dict | None:
    with _ship_state_lock:
        if expected_vehicle_id and _ship_state.get("vehicle_id") != expected_vehicle_id: return None
        if _ship_state.get("lat") is None or _ship_state.get("lon") is None: return None
        return dict(_ship_state)

def _ship_state_is_fresh(ship_state: dict | None) -> bool:
    return False if not ship_state else (time.time() - float(ship_state.get("stamp") or 0.0)) <= SHIP_STATE_TIMEOUT_S

async def ship_state_listener_loop(server_ws_url: str) -> None:
    ui_ws_url = _ui_ws_url(server_ws_url)
    while True:
        try:
            async with websockets.connect(ui_ws_url, ping_interval=10, ping_timeout=10) as ws:
                async for raw_message in ws:
                    try:
                        message = json.loads(raw_message)
                    except json.JSONDecodeError:
                        continue
                    if message.get("op") == "snapshot":
                        for v in message.get("vehicles", []):
                            if v.get("vehicle_type") == "yp": _update_ship_state(v)
                    elif message.get("op") == "vehicle_update":
                        v = message.get("vehicle") or {}
                        if v.get("vehicle_type") == "yp": _update_ship_state(v)
        except Exception as exc:
            await asyncio.sleep(1.0)

def _stop_ship_relative_mission() -> None:
    global _ship_relative_thread
    if _ship_relative_thread and _ship_relative_thread.is_alive():
        _ship_relative_stop_event.set()
        _ship_relative_thread.join(timeout=1.0)
    _ship_relative_stop_event.clear()
    _ship_relative_thread = None

def _launch_ship_relative_mission(master, command_data: dict) -> None:
    global _ship_relative_thread
    if not command_data.get("ship_vehicle_id") or not command_data.get("local_waypoints"): return
    _stop_ship_relative_mission()
    _ship_relative_thread = threading.Thread(target=_run_ship_relative_mission, args=(master, command_data["ship_vehicle_id"], command_data["local_waypoints"], float(command_data.get("arrival_radius_m", SHIP_RELATIVE_DEFAULT_ARRIVAL_RADIUS_M)), float(command_data.get("update_hz", SHIP_RELATIVE_DEFAULT_UPDATE_HZ)), _ship_relative_stop_event), daemon=True)
    _ship_relative_thread.start()

def _run_ship_relative_mission(master, ship_vehicle_id: str, local_waypoints: list, arrival_radius_m: float, update_hz: float, stop_event: threading.Event) -> None:
    update_period_s = 1.0 / max(update_hz, 1.0)
    for index, waypoint in enumerate(local_waypoints, start=1):
        while not stop_event.is_set():
            ship_state, vehicle_state = _snapshot_ship_state(ship_vehicle_id), _snapshot_vehicle_state()
            if not _ship_state_is_fresh(ship_state) or ship_state is None or vehicle_state.get("lat") is None:
                time.sleep(update_period_s)
                continue
            target_lat, target_lon, target_alt = _relative_waypoint_to_global(float(ship_state["lat"]), float(ship_state["lon"]), float(ship_state.get("heading_deg") or 0.0), float(ship_state.get("alt") or 0.0), waypoint)
            if VEHICLE_TYPE in ["usv", "ugv"]: target_alt = 0.0
            master.mav.set_position_target_global_int_send(0, master.target_system, master.target_component, mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, int(0b110111000000), int(target_lat * 1e7), int(target_lon * 1e7), target_alt, float(ship_state.get("vn_ms") or 0.0), float(ship_state.get("ve_ms") or 0.0), 0.0, 0, 0, 0, 0, 0)
            alt_condition_met = True if VEHICLE_TYPE in ["usv", "ugv"] else abs(float(vehicle_state["alt"]) - target_alt) <= max(2.0, arrival_radius_m * 0.5)
            if _distance_m(float(vehicle_state["lat"]), float(vehicle_state["lon"]), target_lat, target_lon) <= arrival_radius_m and alt_condition_met:
                # Only break if there are more waypoints in the sequence
                if index < len(local_waypoints):
                    break
            time.sleep(update_period_s)
        if stop_event.is_set(): return

def goto_waypoint(master, target_lat, target_lon, target_alt, timeout=30, force_guided=True):
    if VEHICLE_TYPE in ["usv", "ugv"]: target_alt = 0.0
    if force_guided: master.set_mode('GUIDED')
    master.mav.set_position_target_global_int_send(0, master.target_system, master.target_component, mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, int(0b110111111000), int(target_lat * 1e7), int(target_lon * 1e7), target_alt, 0, 0, 0, 0, 0, 0, 0, 0)

# --- SAR MISSIONS THREAD TARGETS ---

def _run_search_grid(master, lat: float, lon: float, grid_size_m: float, swath_m: float, altitude_m: float) -> None:
    if VEHICLE_TYPE in ["usv", "ugv"]: altitude_m = 0.0
    with _sar_mission_lock:
        _sar_stop_event.clear()
        try:
            sar_missions.execute_search_grid_streaming(master, lat, lon, grid_size_m, swath_m, altitude_m, include_takeoff=SAR_INCLUDE_TAKEOFF, takeoff_altitude_m=SAR_TAKEOFF_ALT_M, climb_speed_ms=SAR_CLIMB_SPEED_MS, arrival_radius_m=SAR_ARRIVAL_RADIUS_M, stop_event=_sar_stop_event, telemetry_callback=_capture_sar_telemetry)
        except Exception as exc: pass

def _run_mob_search(master, track_points: list, corridor_half_width_m: float, swath_m: float, altitude_m: float, takeoff_altitude_m: float, climb_speed_ms: float) -> None:
    if VEHICLE_TYPE in ["usv", "ugv"]: altitude_m, takeoff_altitude_m = 0.0, 0.0
    with _sar_mission_lock:
        _sar_stop_event.clear()
        try:
            sar_missions.execute_mob_search_streaming(master, track_points, corridor_half_width_m=corridor_half_width_m, swath_m=swath_m, altitude_m=altitude_m, takeoff_altitude_m=takeoff_altitude_m, climb_speed_ms=climb_speed_ms, include_takeoff=SAR_INCLUDE_TAKEOFF, arrival_radius_m=SAR_ARRIVAL_RADIUS_M, stop_event=_sar_stop_event, telemetry_callback=_capture_sar_telemetry)
        except Exception as exc: pass

def _run_mission_plan(master, waypoints: list, auto_arm_start: bool, force_guided_on_complete: bool) -> None:
    with _sar_mission_lock:
        try:
            item_type_to_cmd = {"waypoint": int(mavutil.mavlink.MAV_CMD_NAV_WAYPOINT), "takeoff": int(mavutil.mavlink.MAV_CMD_NAV_TAKEOFF), "loiter_time": int(mavutil.mavlink.MAV_CMD_NAV_LOITER_TIME), "land": int(mavutil.mavlink.MAV_CMD_NAV_LAND), "rtl": int(mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH), "do_jump": int(mavutil.mavlink.MAV_CMD_DO_JUMP)}
            mission_items = []
            for wp in waypoints:
                if not isinstance(wp, dict) or wp.get("latitude") is None or wp.get("longitude") is None: continue
                command_id = int(wp.get("command_id") or item_type_to_cmd.get(str(wp.get("item_type") or "waypoint").lower(), item_type_to_cmd["waypoint"]))
                mission_items.append((float(wp.get("latitude")), float(wp.get("longitude")), 0.0 if VEHICLE_TYPE in ["usv", "ugv"] else float(wp.get("altitude", 30.0)), command_id, float(wp.get("hold_time_s", 0.0)), float(wp.get("acceptance_radius_m", 8.0)), 0.0, float(wp.get("yaw_deg", 0.0) or 0.0)))
            if not mission_items: return
            if force_guided_on_complete: mission_items.append((float(mission_items[-1][0]), float(mission_items[-1][1]), float(mission_items[-1][2]), int(mavutil.mavlink.MAV_CMD_NAV_GUIDED_ENABLE), 1.0, 0.0, 0.0, 0.0))
            if not sar_missions.upload_mission(master, mission_items): return
            if auto_arm_start:
                sar_missions.set_mode(master, "AUTO", wait_for_ack=False)
                time.sleep(0.2)
                sar_missions.arm_vehicle(master)
                time.sleep(0.2)
                sar_missions.start_mission(master)
        except Exception as exc: pass

# --- MAIN TELEMETRY LOOP ---

async def telemetry_loop(current_config: dict) -> None:
    global VEHICLE_TYPE, SAR_INCLUDE_TAKEOFF

    vehicle_id = current_config["vehicle_id"]
    server_ws_url = current_config["server_ws_url"]
    mavlink_url = current_config["mavlink_url"]
    mavlink_baud = current_config["mavlink_baud"]
    send_hz = current_config["send_hz"]

    system_status["cube_status"] = "Connecting..."
    system_status["cube_connected"] = False

    try:
        master = mavutil.mavlink_connection(mavlink_url, baud=mavlink_baud)
        
        # Non-blocking heartbeat loop
        msg = None
        while not msg:
            msg = master.recv_match(type='HEARTBEAT', blocking=False)
            if not msg:
                await asyncio.sleep(0.5)
                
        system_status["cube_connected"] = True
        system_status["cube_status"] = "Connected"
        system_status["last_hb_time"] = time.time()
        try:
            system_status["flight_mode"] = master.flightmode
        except Exception: pass

        if msg.type in [10, 22]:
            VEHICLE_TYPE = "usv"
            SAR_INCLUDE_TAKEOFF = False
        
        master.mav.request_data_stream_send(master.target_system, master.target_component, mavutil.mavlink.MAV_DATA_STREAM_POSITION, int(send_hz), 1)
        master.mav.request_data_stream_send(master.target_system, master.target_component, mavutil.mavlink.MAV_DATA_STREAM_EXTENDED_STATUS, 2, 1)

        async with websockets.connect(f"{server_ws_url.rstrip('/')}/{vehicle_id}", ping_interval=10, ping_timeout=10) as ws:
            system_status["ws_connected"] = True
            system_status["ws_status"] = "Connected"
            last_send_time = time.time()
            last_video_send_time = 0.0
           
            while True:
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=0.01)
                    try:
                        server_msg = json.loads(response)
                        if server_msg.get("op") == "command" and server_msg.get("vehicle_id") == vehicle_id:
                            command_data = server_msg.get("command", {})
                            cmd_type = command_data.get("type")

                            if cmd_type == "waypoint" and None not in (command_data.get("target", {}).get("latitude"), command_data.get("target", {}).get("longitude"), command_data.get("target", {}).get("altitude")):
                                goto_waypoint(master, command_data["target"]["latitude"], command_data["target"]["longitude"], command_data["target"]["altitude"], force_guided=(server_msg.get("source") != "rtb_follow"))
                            elif cmd_type == "search_grid" and None not in (command_data.get("lat"), command_data.get("lon")):
                                threading.Thread(target=_run_search_grid, args=(master, float(command_data["lat"]), float(command_data["lon"]), float(command_data.get("grid_size_m", 200)), float(command_data.get("swath_m", 20)), float(command_data.get("altitude_m", 30))), daemon=True).start()
                            elif cmd_type == "mob" and len(command_data.get("track_points", [])) >= 2:
                                threading.Thread(target=_run_mob_search, args=(master, command_data["track_points"], float(command_data.get("corridor_half_width_m", 50.0)), float(command_data.get("swath_m", 20.0)), float(command_data.get("altitude_m", 30.0)), float(command_data.get("takeoff_altitude_m", SAR_TAKEOFF_ALT_M)), float(command_data.get("climb_speed_ms", SAR_CLIMB_SPEED_MS))), daemon=True).start()
                            elif cmd_type == "cancel_sar":
                                _sar_stop_event.set()
                            elif cmd_type == "ship_relative_trajectory":
                                _launch_ship_relative_mission(master, command_data)
                            elif cmd_type == "mission_plan" and isinstance(command_data.get("waypoints", []), list):
                                threading.Thread(target=_run_mission_plan, args=(master, command_data["waypoints"], bool(command_data.get("auto_arm_start", True)), bool(command_data.get("force_guided_on_complete", False))), daemon=True).start()
                            elif cmd_type == "set_mode" and command_data.get("mode"):
                                sar_missions.set_mode(master, str(command_data["mode"]), wait_for_ack=False)
                    except json.JSONDecodeError: pass
                except asyncio.TimeoutError: pass

                msg = None
                if not _sar_mission_lock.locked():
                    msg = master.recv_match(type=["GLOBAL_POSITION_INT", "HEARTBEAT", "GPS_RAW_INT"], blocking=False)
               
                now = time.time()
                if system_status["cube_connected"] and (now - system_status["last_hb_time"] > 5.0):
                    print("\n[WARNING] Heartbeat timeout or socket dead. Forcing reconnect...")
                    reconnect_event.set()
                    break
                telemetry_sample = None
                if msg is not None:
                    msg_type = msg.get_type()
                    if msg_type == "HEARTBEAT":
                        system_status["last_hb_time"] = now
                        try:
                            system_status["flight_mode"] = master.flightmode
                        except Exception: pass
                    elif msg_type == "GPS_RAW_INT":
                        system_status["gps_status"] = get_gps_fix_label(getattr(msg, "fix_type", 0))
                        system_status["satellites"] = getattr(msg, "satellites_visible", 0)
                    elif msg_type == "GLOBAL_POSITION_INT":
                        lat, lon, alt = msg.lat / 1e7, msg.lon / 1e7, msg.relative_alt / 1000.0
                        heading_raw = getattr(msg, "hdg", None)
                        heading = (heading_raw / 100.0) if heading_raw is not None and heading_raw != 65535 else None
                        _update_vehicle_state(lat, lon, alt, heading)
                        telemetry_sample = (lat, lon, alt, heading)
                else:
                    telemetry_sample = _snapshot_sar_telemetry()
                    if telemetry_sample is not None: _update_vehicle_state(*telemetry_sample)

                if telemetry_sample is not None and (now - last_send_time) >= (1.0 / send_hz):
                    await ws.send(json.dumps(create_navsatfix_message(vehicle_id, *telemetry_sample)))
                    last_send_time = now

                if now - last_video_send_time >= 60.0:
                    await ws.send(json.dumps(create_video_stream_message(vehicle_id, WEBRTC_IP)))
                    last_video_send_time = now

                await asyncio.sleep(0.01)

    except Exception as exc:
        system_status["ws_connected"] = False
        system_status["ws_status"] = "Disconnected"
        traceback.print_exc()

async def main():
    global config
    config = load_config()

    await start_web_server()

    while True:
        reconnect_event.clear()
        current_config = config.copy()

        telemetry_task = asyncio.create_task(telemetry_loop(current_config))
        ship_task = asyncio.create_task(ship_state_listener_loop(current_config["server_ws_url"]))

        await reconnect_event.wait()
        
        print("Settings updated via web UI! Terminating connections to reconnect...")
        telemetry_task.cancel()
        ship_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down cleanly")