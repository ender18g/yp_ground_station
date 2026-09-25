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

# --- DEFAULT CONFIGURATION FOR APACHE 3 (USV) ---
DEFAULT_CONFIG = {
    "server_ws_url": os.getenv("SERVER_WS_URL", "ws://10.10.130.2:8000/ws/vehicle"),
    "vehicle_id": os.getenv("VEHICLE_ID", "apache3"),
    "mavlink_url": os.getenv("MAVLINK_URL", "tcp:192.168.53.254:30000"),
    "mavlink_baud": int(os.getenv("MAVLINK_BAUD", "115200")),
    "send_hz": float(os.getenv("SEND_HZ", "5")),
    "video_url": os.getenv("VIDEO_URL", ""),
    "web_port": 8880,  # Uses 8880 to avoid conflict with local GCS Docker container on 8080
}

config = {}
reconnect_event = asyncio.Event()

mav_master = None
mav_master_lock = threading.Lock()

system_status = {
    "cube_connected": False,
    "cube_status": "Connecting...",
    "ws_connected": False,
    "ws_status": "Connecting...",
    "flight_mode": "UNKNOWN",
    "available_modes": [],
    "gps_status": "No Fix",
    "satellites": 0,
    "last_hb_time": 0,
}

# --- USV & CHCNAV CONSTANTS ---
VEHICLE_TYPE = "usv"
WEBRTC_IP = _resolve_webrtc_ip()

CUSTOM_MODE_MANUAL = 0
CUSTOM_MODE_HOLD = 4
CUSTOM_MODE_AUTO = 10
CUSTOM_MODE_RTL = 11
CUSTOM_MODE_LOITER = 12
CUSTOM_MODE_GUIDED = 15

SAR_TAKEOFF_ALT_M = 0.0
SAR_CLIMB_SPEED_MS = 0.0
SAR_INCLUDE_TAKEOFF = False
SAR_STREAMING_MODE = False  # CHCNav requires full mission batch uploads
SAR_ARRIVAL_RADIUS_M = float(os.getenv("SAR_ARRIVAL_RADIUS_M", "10.0"))

_sar_mission_lock = threading.Lock()
_sar_stop_event = threading.Event()
_sar_telemetry_lock = threading.Lock()
_sar_latest_nav = {"lat": None, "lon": None, "alt": 0.0, "heading": None, "stamp": 0.0}

SHIP_STATE_TIMEOUT_S = float(os.getenv("SHIP_STATE_TIMEOUT_S", "2.0"))
SHIP_RELATIVE_DEFAULT_UPDATE_HZ = float(os.getenv("SHIP_RELATIVE_UPDATE_HZ", "10.0"))
SHIP_RELATIVE_DEFAULT_ARRIVAL_RADIUS_M = float(os.getenv("SHIP_RELATIVE_ARRIVAL_RADIUS_M", "6.0"))
EARTH_RADIUS_M = 6_378_137.0

_vehicle_state_lock = threading.Lock()
_vehicle_state = {"lat": None, "lon": None, "alt": 0.0, "heading_deg": None, "stamp": 0.0}
_ship_state_lock = threading.Lock()
_ship_state = {"vehicle_id": None, "lat": None, "lon": None, "alt": 0.0, "heading_deg": None, "vn_ms": 0.0, "ve_ms": 0.0, "stamp": 0.0}

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
    <title>Apache 3 Bridge - Telemetry Diagnostics & Config</title>
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
        .hint {{ font-size: 0.8rem; color: #64748b; margin-top: 4px; }}
        button {{ width: 100%; margin-top: 25px; padding: 12px; background: #2563eb; color: white; border: none; border-radius: 6px; font-size: 1rem; font-weight: bold; cursor: pointer; }}
        button:hover {{ background: #1d4ed8; }}
        .mode-btn {{ background: #0284c7; margin-top: 10px; }}
        .mode-btn:hover {{ background: #0369a1; }}
        .status-msg {{ font-size: 0.85rem; margin-top: 8px; font-weight: bold; text-align: center; }}
    </style>
</head>
<body>
    <h2>System Diagnostics</h2>
    <div class="diag-card">
        <div class="diag-grid">
            <div class="diag-item">
                <div class="diag-label">Apache Controller</div>
                <div class="diag-value"><span id="cube_status" class="badge badge-offline">Offline</span></div>
            </div>
            <div class="diag-item">
                <div class="diag-label">GCS WebServer</div>
                <div class="diag-value"><span id="ws_status" class="badge badge-offline">Offline</span></div>
            </div>
            <div class="diag-item">
                <div class="diag-label">Active Drive Mode</div>
                <div class="diag-value" id="flight_mode" style="color: #38bdf8;">UNKNOWN</div>
            </div>
            <div class="diag-item">
                <div class="diag-label">GPS Status</div>
                <div class="diag-value" id="gps_status" style="color: #f59e0b;">No Fix</div>
            </div>
        </div>
    </div>

    <h2>Drive Mode Control</h2>
    <div class="diag-card">
        <label style="margin-top:0;">Request Mode Change</label>
        <select id="mode_select">
            <option value="10">AUTO (10)</option>
            <option value="0">MANUAL (0)</option>
            <option value="12">LOITER / Station Keeping (12)</option>
            <option value="11">RTL / Return to Home (11)</option>
            <option value="4">HOLD (4)</option>
        </select>
        <button type="button" class="mode-btn" onclick="sendModeChange()">Change Mode Now</button>
        <div id="mode_msg" class="status-msg"></div>
    </div>

    <h2>Configuration</h2>
    <form method="POST" action="/save">
        <label>GCS Server WebSocket URL</label>
        <input type="text" name="server_ws_url" value="{server_ws_url}" required>

        <label>Vehicle ID</label>
        <input type="text" name="vehicle_id" value="{vehicle_id}" required>

        <label>MAVLink Connection URL</label>
        <select id="mavlink_url_select" name="mavlink_url_select" onchange="toggleCustomUrl()">
            <option value="tcp:192.168.53.254:30000" {s_tcp}>tcp:192.168.53.254:30000 (Apache TCP Default)</option>
            <option value="udpin:0.0.0.0:14551" {s_udp}>udpin:0.0.0.0:14551 (UDP Listen Default)</option>
            <option value="custom" {s_custom}>Custom Endpoint...</option>
        </select>
        <input type="text" id="mavlink_url_custom" name="mavlink_url_custom" value="{mavlink_url_custom}" style="display: {custom_display}; margin-top: 8px;" placeholder="e.g. tcp:192.168.1.168:30000">

        <label>Update Rate (Hz)</label>
        <input type="number" step="0.1" name="send_hz" value="{send_hz}" required>

        <label>Video Endpoint URL</label>
        <input type="text" name="video_url" value="{video_url}" placeholder="{video_url_placeholder}">
        <div class="hint">Leave blank to auto-use MediaMTX WebRTC stream ({webrtc_ip}:8889).</div>

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

        async function sendModeChange() {{
            const select = document.getElementById('mode_select');
            const msgElem = document.getElementById('mode_msg');
            const targetMode = parseInt(select.value);

            msgElem.style.color = '#38bdf8';
            msgElem.innerText = 'Sending mode change request...';

            try {{
                const res = await fetch('/api/mode', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ mode_id: targetMode }})
                }});
                const data = await res.json();

                if (res.ok && data.ok) {{
                    msgElem.style.color = '#34d399';
                    msgElem.innerText = 'SUCCESS: Mode updated';
                }} else {{
                    msgElem.style.color = '#fda4af';
                    msgElem.innerText = 'FAILED: ' + (data.error || 'Request rejected');
                }}
            }} catch (e) {{
                msgElem.style.color = '#fda4af';
                msgElem.innerText = 'ERROR: Unable to communicate with bridge server';
            }}
        }}

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

def _default_video_url() -> str:
    return f"http://{WEBRTC_IP}:8889/apache3/whep"

async def handle_index(request):
    url = config["mavlink_url"]
    known_endpoints = ["tcp:192.168.53.254:30000", "udpin:0.0.0.0:14551"]
    is_custom = url not in known_endpoints

    html = HTML_TEMPLATE.format(
        server_ws_url=config["server_ws_url"],
        vehicle_id=config["vehicle_id"],
        s_tcp="selected" if url == "tcp:192.168.53.254:30000" else "",
        s_udp="selected" if url == "udpin:0.0.0.0:14551" else "",
        s_custom="selected" if is_custom else "",
        mavlink_url_custom=url if is_custom else "",
        custom_display="block" if is_custom else "none",
        send_hz=config["send_hz"],
        video_url=config.get("video_url", ""),
        video_url_placeholder=_default_video_url(),
        webrtc_ip=WEBRTC_IP,
    )
    return web.Response(text=html, content_type="text/html")

async def handle_status_api(request):
    if time.time() - system_status["last_hb_time"] > 4.0:
        system_status["cube_connected"] = False
        system_status["cube_status"] = "Heartbeat Lost"
    return web.json_response(system_status)

async def handle_mode_api(request):
    try:
        body = await request.json()
        target_mode_id = int(body.get("mode_id", CUSTOM_MODE_AUTO))

        with mav_master_lock:
            if not mav_master or not system_status["cube_connected"]:
                return web.json_response({"ok": False, "error": "Apache controller disconnected"}, status=503)

            set_chcnav_mode(mav_master, target_mode_id)
            return web.json_response({"ok": True, "mode_id": target_mode_id})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

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
    config["send_hz"] = float(data.get("send_hz", config["send_hz"]))
    config["video_url"] = data.get("video_url", config.get("video_url", "")).strip()

    save_config(config)
    reconnect_event.set()
    return web.HTTPFound(location="/")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status_api)
    app.router.add_post("/api/mode", handle_mode_api)
    app.router.add_post("/save", handle_save)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.get("web_port", 8880))
    await site.start()
    print(f"Web interface running at http://0.0.0.0:{config.get('web_port', 8880)}")

# --- CHCNAV MAVLINK MISSION & COMMAND FUNCTIONS ---

def set_chcnav_mode(master, mode_id: int):
    """Sets mode using CHCNav custom_mode integer values."""
    master.mav.set_mode_send(
        master.target_system,
        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        mode_id
    )

def upload_mission_batch(master, waypoints: list[tuple[float, float]], append_terminal_loiter: bool = True) -> bool:
    """
    Uploads waypoints using CHCNav's MISSION_COUNT -> MISSION_ITEM handshake protocol.
    Appends an Unlimited Loiter (Command 17) item at the final coordinate so the boat
    automatically holds station upon completing the route.
    """
    target_sys = master.target_system or 1
    target_comp = master.target_component or 1

    formatted_wps = list(waypoints)
    if append_terminal_loiter and formatted_wps:
        formatted_wps.append(formatted_wps[-1])  # Repeat final position for Loiter item

    count = len(formatted_wps) + 1  # Include Home Point at seq 0

    print(f"[MISSION] Starting upload of {count} items to Apache USV...")
    master.mav.mission_count_send(target_sys, target_comp, count)

    for i in range(count):
        msg = master.recv_match(type=['MISSION_REQUEST', 'MISSION_REQUEST_INT'], blocking=True, timeout=5.0)
        if not msg:
            print(f"[MISSION] Timeout waiting for item request {i}")
            return False

        seq = msg.seq
        if seq == 0:
            lat, lon = formatted_wps[0]
            frame = mavutil.mavlink.MAV_FRAME_GLOBAL
            cmd_id = mavutil.mavlink.MAV_CMD_NAV_WAYPOINT
        elif append_terminal_loiter and seq == count - 1:
            lat, lon = formatted_wps[seq - 1]
            frame = mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT
            cmd_id = 17  # MAV_CMD_NAV_LOITER_UNLIM
        else:
            lat, lon = formatted_wps[seq - 1]
            frame = mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT
            cmd_id = mavutil.mavlink.MAV_CMD_NAV_WAYPOINT

        master.mav.mission_item_send(
            target_sys,
            target_comp,
            seq,
            frame,
            cmd_id,
            0, 1, 0, 0, 0, 0,
            lat, lon, 0.0
        )

    ack = master.recv_match(type='MISSION_ACK', blocking=True, timeout=5.0)
    if ack and ack.type == mavutil.mavlink.MAV_MISSION_ACCEPTED:
        print("[MISSION] Upload acknowledged and accepted!")
        return True
    print(f"[MISSION] Upload failed or rejected with ACK type: {getattr(ack, 'type', 'None')}")
    return False

def _run_single_waypoint(master, lat: float, lon: float):
    with _sar_mission_lock:
        if upload_mission_batch(master, [(lat, lon)], append_terminal_loiter=True):
            time.sleep(0.2)
            set_chcnav_mode(master, CUSTOM_MODE_AUTO)

def _run_search_grid(master, waypoints: list[tuple[float, float]]):
    with _sar_mission_lock:
        if upload_mission_batch(master, waypoints, append_terminal_loiter=True):
            time.sleep(0.2)
            set_chcnav_mode(master, CUSTOM_MODE_AUTO)

def get_gps_fix_label(fix_type: int) -> str:
    fix_map = {0: "No GPS", 1: "No Fix", 2: "2D Fix", 3: "3D Fix", 4: "DGPS", 5: "RTK Float", 6: "RTK Fixed"}
    return fix_map.get(fix_type, f"Fix {fix_type}")

def create_gps_fix_message(vehicle_id: str, msg) -> dict:
    eph = getattr(msg, "eph", 65535)
    epv = getattr(msg, "epv", 65535)
    h_acc = getattr(msg, "h_acc", None)
    v_acc = getattr(msg, "v_acc", None)
    fix_type = getattr(msg, "fix_type", 0)
    satellites_visible = getattr(msg, "satellites_visible", 255)
    return {
        "vehicle_id": vehicle_id,
        "vehicle_type": VEHICLE_TYPE,
        "topic": f"/vehicles/{vehicle_id}/gps_fix",
        "type": "mavlink/GPS_RAW_INT",
        "stamp": time.time(),
        "msg": {
            "fix_type": fix_type,
            "fix_type_label": get_gps_fix_label(fix_type),
            "satellites_visible": satellites_visible if satellites_visible != 255 else None,
            "horizontal_accuracy_m": (h_acc / 1000.0) if h_acc else ((eph / 100.0) if eph != 65535 else None),
            "vertical_accuracy_m": (v_acc / 1000.0) if v_acc else ((epv / 100.0) if epv != 65535 else None),
        },
    }

def create_navsatfix_message(vehicle_id: str, lat: float, lon: float, heading: float | None = None) -> dict:
    now = time.time()
    sec, nanosec = int(now), int((now - int(now)) * 1e9)
    payload = {
        "vehicle_id": vehicle_id,
        "vehicle_type": VEHICLE_TYPE,
        "topic": f"/vehicles/{vehicle_id}/navsatfix",
        "type": "sensor_msgs/msg/NavSatFix",
        "stamp": now,
        "msg": {
            "header": {"stamp": {"sec": sec, "nanosec": nanosec}, "frame_id": "map"},
            "status": {"status": 0, "service": 1},
            "latitude": lat, "longitude": lon, "altitude": 0.0,
            "position_covariance": [0.0] * 9, "position_covariance_type": 0,
        },
    }
    if heading is not None:
        payload["msg"]["heading"] = heading
    return payload

def create_video_stream_message(vehicle_id: str, video_url: str) -> dict:
    return {
        "op": "video_stream_update",
        "video": {
            "vehicle_id": vehicle_id,
            "enabled": True,
            "streams": [{"label": "Apache Video Stream", "url": video_url}]
        }
    }

def _ui_ws_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    marker = "/ws/vehicle"
    if marker in base:
        return f"{base.split(marker, 1)[0]}/ws/ship_state"
    return base

# --- MAIN TELEMETRY & COMMAND LOOP ---

async def telemetry_loop(current_config: dict) -> None:
    global mav_master

    vehicle_id = current_config["vehicle_id"]
    server_ws_url = current_config["server_ws_url"]
    mavlink_url = current_config["mavlink_url"]
    send_hz = current_config["send_hz"]
    video_url = current_config.get("video_url") or _default_video_url()

    system_status["cube_status"] = "Connecting..."
    system_status["cube_connected"] = False

    ws = None  # declared here so the finally block can always close it
    try:
        master = mavutil.mavlink_connection(mavlink_url)
        with mav_master_lock:
            mav_master = master

        msg = None
        while not msg:
            msg = master.recv_match(type='HEARTBEAT', blocking=False)
            if not msg:
                await asyncio.sleep(0.5)

        system_status["cube_connected"] = True
        system_status["cube_status"] = "Connected"
        system_status["last_hb_time"] = time.time()
        
        # Decode active mode from custom_mode integer
        custom_mode_val = getattr(msg, "custom_mode", 0)
        mode_names = {0: "MANUAL", 4: "HOLD", 10: "AUTO", 11: "RTL", 12: "LOITER", 15: "GUIDED"}
        system_status["flight_mode"] = mode_names.get(custom_mode_val, f"MODE_{custom_mode_val}")

        master.mav.request_data_stream_send(master.target_system, master.target_component, mavutil.mavlink.MAV_DATA_STREAM_POSITION, int(send_hz), 1)
        master.mav.request_data_stream_send(master.target_system, master.target_component, mavutil.mavlink.MAV_DATA_STREAM_EXTENDED_STATUS, 2, 1)
        master.mav.command_long_send(master.target_system, master.target_component, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0, mavutil.mavlink.MAVLINK_MSG_ID_GPS_RAW_INT, int(1e6 / 2), 0, 0, 0, 0, 0)
        master.mav.command_long_send(master.target_system, master.target_component, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0, mavutil.mavlink.MAVLINK_MSG_ID_GPS2_RAW, int(1e6 / 2), 0, 0, 0, 0, 0)

        # Non-blocking WebSocket state variables
        ws_last_connect_attempt = 0.0
        last_send_time = time.time()
        last_video_send_time = 0.0

        while True:
            now = time.time()

            # --- 1. Manage WebSocket Reconnection ---
            if ws is None and (now - ws_last_connect_attempt > 5.0):
                ws_last_connect_attempt = now
                try:
                    # Try to connect with a short 2-second timeout so it doesn't block MAVLink reading
                    ws = await asyncio.wait_for(
                        websockets.connect(f"{server_ws_url.rstrip('/')}/{vehicle_id}", ping_interval=10, ping_timeout=10),
                        timeout=2.0
                    )
                    system_status["ws_connected"] = True
                    system_status["ws_status"] = "Connected"
                    print("[WS] Successfully connected to GCS server.")
                except Exception as e:
                    system_status["ws_connected"] = False
                    system_status["ws_status"] = "Server Offline (Retrying)"
                    ws = None

            # --- 2. Read WebSocket Commands ---
            if ws is not None:
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=0.01)
                    try:
                        server_msg = json.loads(response)
                        if server_msg.get("op") == "command" and server_msg.get("vehicle_id") == vehicle_id:
                            command_data = server_msg.get("command", {})
                            cmd_type = command_data.get("type")

                            if cmd_type == "waypoint":
                                target = command_data.get("target", {})
                                if "latitude" in target and "longitude" in target:
                                    threading.Thread(
                                        target=_run_single_waypoint,
                                        args=(master, float(target["latitude"]), float(target["longitude"])),
                                        daemon=True
                                    ).start()

                            elif cmd_type == "search_grid":
                                grid_wps = command_data.get("waypoints", [])
                                if grid_wps:
                                    wps_tuples = [(float(pt[0]), float(pt[1])) for pt in grid_wps if len(pt) >= 2]
                                    threading.Thread(
                                        target=_run_search_grid,
                                        args=(master, wps_tuples),
                                        daemon=True
                                    ).start()

                            elif cmd_type == "rtb":
                                set_chcnav_mode(master, CUSTOM_MODE_RTL)

                            elif cmd_type == "cancel_sar":
                                _sar_stop_event.set()
                                set_chcnav_mode(master, CUSTOM_MODE_LOITER)

                            elif cmd_type == "set_mode":
                                mode_str = str(command_data.get("mode", "")).upper()
                                str_map = {"MANUAL": CUSTOM_MODE_MANUAL, "AUTO": CUSTOM_MODE_AUTO, "RTL": CUSTOM_MODE_RTL, "LOITER": CUSTOM_MODE_LOITER, "HOLD": CUSTOM_MODE_HOLD}
                                if mode_str in str_map:
                                    set_chcnav_mode(master, str_map[mode_str])

                            elif cmd_type == "rtcm_data":
                                flags = command_data.get("flags", 0)
                                data_len = command_data.get("len", 0)
                                raw_data = command_data.get("data", [])
                                if master and data_len > 0:
                                    padded_payload = bytearray(raw_data + [0] * (180 - len(raw_data)))
                                    try:
                                        master.mav.gps_rtcm_data_send(flags, data_len, padded_payload)
                                    except Exception as e:
                                        print(f"[RTCM] MAVLink send error: {e}")

                    except json.JSONDecodeError: pass
                except asyncio.TimeoutError:
                    pass
                except (websockets.exceptions.ConnectionClosed, ConnectionError) as e:
                    print(f"[WS] Connection dropped: {e}")
                    ws = None
                    system_status["ws_connected"] = False
                    system_status["ws_status"] = "Disconnected"

            # --- 3. Read MAVLink (Always runs) ---
            msg = None
            if not _sar_mission_lock.locked():
                msg = master.recv_match(type=["GLOBAL_POSITION_INT", "HEARTBEAT", "GPS_RAW_INT", "GPS2_RAW"], blocking=False)

            now = time.time()
            if system_status["cube_connected"] and (now - system_status["last_hb_time"] > 5.0):
                reconnect_event.set()
                break

            telemetry_sample = None
            if msg is not None:
                msg_type = msg.get_type()
                if msg_type == "HEARTBEAT":
                    system_status["last_hb_time"] = now
                    custom_mode_val = getattr(msg, "custom_mode", 0)
                    mode_names = {0: "MANUAL", 4: "HOLD", 10: "AUTO", 11: "RTL", 12: "LOITER", 15: "GUIDED"}
                    system_status["flight_mode"] = mode_names.get(custom_mode_val, f"MODE_{custom_mode_val}")
                elif msg_type in ("GPS_RAW_INT", "GPS2_RAW"):
                    system_status["gps_status"] = get_gps_fix_label(getattr(msg, "fix_type", 0))
                    system_status["satellites"] = getattr(msg, "satellites_visible", 0)
                    if ws is not None:
                        try:
                            await ws.send(json.dumps(create_gps_fix_message(vehicle_id, msg)))
                        except Exception: pass
                elif msg_type == "GLOBAL_POSITION_INT":
                    lat, lon = msg.lat / 1e7, msg.lon / 1e7
                    heading_raw = getattr(msg, "hdg", None)
                    heading = (heading_raw / 100.0) if heading_raw is not None and heading_raw != 65535 else None
                    telemetry_sample = (lat, lon, heading)

            # --- 4. Send WebSocket Telemetry ---
            if ws is not None:
                try:
                    if telemetry_sample is not None and (now - last_send_time) >= (1.0 / send_hz):
                        await ws.send(json.dumps(create_navsatfix_message(vehicle_id, *telemetry_sample)))
                        last_send_time = now

                    if now - last_video_send_time >= 60.0:
                        await ws.send(json.dumps(create_video_stream_message(vehicle_id, video_url)))
                        last_video_send_time = now
                except Exception as e:
                    print(f"[WS] Data send failed: {e}")
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    ws = None
                    system_status["ws_connected"] = False
                    system_status["ws_status"] = "Disconnected"

            await asyncio.sleep(0.01)

    except Exception as exc:
        system_status["ws_connected"] = False
        system_status["ws_status"] = "Disconnected"
        traceback.print_exc()
    finally:
        # Guarantee the socket is released on break, exception, or task cancellation.
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

async def main():
    global config
    config = load_config()

    await start_web_server()

    while True:
        reconnect_event.clear()
        current_config = config.copy()

        telemetry_task = asyncio.create_task(telemetry_loop(current_config))
        await reconnect_event.wait()

        print("Settings updated via web UI! Restarting telemetry connections...")
        telemetry_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down cleanly")