import asyncio
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from aiohttp import web
import websockets
from pymavlink import mavutil

CONFIG_PATH = Path("config.json")

# Default Fallback Configuration
DEFAULT_CONFIG = {
    "server_ws_url": "ws://192.168.1.100:8000/ws/vehicle",
    "vehicle_id": "yp",
    "serial_port": "/dev/ttyACM0",
    "baud_rate": 115200,
    "send_hz": 5.0,
    "web_port": 8080,
}

config = {}
reconnect_event = asyncio.Event()
telemetry_queue = asyncio.Queue(maxsize=50)

# Shared Live Telemetry & Connection Status
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

# --- Configuration Persistence ---

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

# --- Web Server Handler & HTML Page ---

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Raspberry Pi - Telemetry Diagnostics & Config</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; padding: 20px; max-width: 550px; margin: 0 auto; }}
        h2 {{ color: #38bdf8; border-bottom: 2px solid #334155; padding-bottom: 10px; margin-bottom: 15px; }}
        
        /* Diagnostics Panel */
        .diag-card {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 15px; margin-bottom: 25px; }}
        .diag-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 10px; }}
        .diag-item {{ background: #0f172a; padding: 10px; border-radius: 6px; border: 1px solid #1e293b; }}
        .diag-label {{ font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px; }}
        .diag-value {{ font-size: 1.05rem; font-weight: 600; margin-top: 4px; }}
        
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.85rem; font-weight: 600; }}
        .badge-online {{ background: #065f46; color: #34d399; }}
        .badge-offline {{ background: #881337; color: #fda4af; }}

        /* Form Controls */
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
        <input type="text" name="server_ws_url" value="{server_ws_url}" required placeholder="ws://192.168.1.100:8000/ws/vehicle">

        <label>Vehicle ID</label>
        <input type="text" name="vehicle_id" value="{vehicle_id}" required>

        <label>Serial Port (Cube microUSB)</label>
        <input type="text" name="serial_port" value="{serial_port}" required placeholder="/dev/ttyACM0">

        <label>Baud Rate</label>
        <select name="baud_rate">
            <option value="115200" {b115200}>115200 (USB Default)</option>
            <option value="57600" {b57600}>57600 (Telemetry Radio)</option>
            <option value="9600" {b9600}>9600</option>
        </select>

        <label>Update Rate (Hz)</label>
        <input type="number" step="0.1" name="send_hz" value="{send_hz}" required>

        <button type="submit">Save & Restart Telemetry Stream</button>
    </form>

    <script>
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
            }} catch (e) {{
                console.error("Failed fetching status", e);
            }}
        }}
        setInterval(fetchStatus, 1000);
        fetchStatus();
    </script>
</body>
</html>
"""

async def handle_index(request):
    html = HTML_TEMPLATE.format(
        server_ws_url=config["server_ws_url"],
        vehicle_id=config["vehicle_id"],
        serial_port=config["serial_port"],
        send_hz=config["send_hz"],
        b115200="selected" if config["baud_rate"] == 115200 else "",
        b57600="selected" if config["baud_rate"] == 57600 else "",
        b9600="selected" if config["baud_rate"] == 9600 else "",
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
    config["server_ws_url"] = data.get("server_ws_url", config["server_ws_url"]).strip()
    config["vehicle_id"] = data.get("vehicle_id", config["vehicle_id"]).strip()
    config["serial_port"] = data.get("serial_port", config["serial_port"]).strip()
    config["baud_rate"] = int(data.get("baud_rate", config["baud_rate"]))
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

# --- Helpers ---

def ros_stamp() -> tuple[int, int, float]:
    stamp = time.time()
    sec = int(stamp)
    return sec, int((stamp - sec) * 1_000_000_000), stamp

def yaw_to_quaternion(yaw_deg: float) -> dict[str, float]:
    half = math.radians(yaw_deg) / 2.0
    return {"x": 0.0, "y": 0.0, "z": math.sin(half), "w": math.cos(half)}

def wrap(vehicle_id: str, topic_suffix: str, msg_type: str, stamp: float, msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "vehicle_id": vehicle_id,
        "vehicle_type": "yp",
        "topic": f"/vehicles/{vehicle_id}/{topic_suffix}",
        "type": msg_type,
        "stamp": stamp,
        "msg": msg,
    }

def get_gps_fix_label(fix_type: int) -> str:
    fix_map = {0: "No GPS", 1: "No Fix", 2: "2D Fix", 3: "3D Fix", 4: "DGPS", 5: "RTK Float", 6: "RTK Fixed"}
    return fix_map.get(fix_type, f"Fix {fix_type}")

# --- Decoupled Connection Tasks ---

def queue_telemetry(vehicle_id: str, lat: float, lon: float, alt: float, heading: float, speed: float):
    sec, nanosec, stamp = ros_stamp()
    messages = [
        wrap(vehicle_id, "heartbeat", "yp_ground_station/msg/Heartbeat", stamp, {"mode": system_status["flight_mode"], "armed": True}),
        wrap(
            vehicle_id, "navsatfix", "sensor_msgs/msg/NavSatFix", stamp,
            {
                "header": {"stamp": {"sec": sec, "nanosec": nanosec}, "frame_id": "yp_gps"},
                "status": {"status": 0, "service": 1},
                "latitude": lat, "longitude": lon, "altitude": alt,
                "position_covariance": [0.0] * 9, "position_covariance_type": 0,
                "heading": heading, "speed_mps": speed,
            },
        ),
        wrap(
            vehicle_id, "pose", "geometry_msgs/msg/Pose", stamp,
            {
                "position": {"x": 0.0, "y": 0.0, "z": alt},
                "orientation": yaw_to_quaternion(heading),
                "heading": heading,
            },
        ),
    ]
    for msg in messages:
        try:
            telemetry_queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass # Drop oldest messages if WebSocket is down and queue is full

async def ws_loop(current_config: dict):
    base_url = current_config["server_ws_url"].rstrip("/")
    vehicle_id = current_config["vehicle_id"]
    uri = f"{base_url}/{vehicle_id}"

    while not reconnect_event.is_set():
        system_status["ws_status"] = "Connecting..."
        system_status["ws_connected"] = False
        try:
            async with websockets.connect(uri, ping_interval=30, ping_timeout=20) as ws:
                system_status["ws_connected"] = True
                system_status["ws_status"] = "Connected"
                print(f"WebSocket Connected to {uri}!")
                
                # Continuously pull from the queue and send
                while not reconnect_event.is_set():
                    msg = await telemetry_queue.get()
                    await ws.send(json.dumps(msg))
                    
        except Exception as exc:
            system_status["ws_connected"] = False
            system_status["ws_status"] = "Disconnected"
            # Flush the queue so it doesn't build up stale data while offline
            while not telemetry_queue.empty():
                telemetry_queue.get_nowait()
            await asyncio.sleep(2.0)

async def mavlink_loop(current_config: dict):
    port = current_config["serial_port"]
    baud = current_config["baud_rate"]
    hz = current_config["send_hz"]
    v_id = current_config["vehicle_id"]

    while not reconnect_event.is_set():
        system_status["cube_status"] = "Connecting..."
        system_status["cube_connected"] = False
        print(f"Connecting to Cube on {port} at {baud} baud...")

        try:
            master = mavutil.mavlink_connection(port, baud=baud)
        except Exception as e:
            system_status["cube_connected"] = False
            system_status["cube_status"] = f"Port Error"
            print(f"Serial port connection error: {e}")
            await asyncio.sleep(2.0)
            continue

        # Non-blocking heartbeat wait
        connected = False
        while not reconnect_event.is_set():
            hb = master.recv_match(type="HEARTBEAT", blocking=False)
            if hb:
                master.target_system = hb.get_srcSystem()
                master.target_component = hb.get_srcComponent()
                system_status["cube_connected"] = True
                system_status["cube_status"] = "Connected"
                system_status["last_hb_time"] = time.time()
                try:
                    system_status["flight_mode"] = master.flightmode
                except Exception:
                    pass
                print(f"Heartbeat received! System: {master.target_system}, Component: {master.target_component}")
                connected = True
                break
            await asyncio.sleep(0.1)

        if reconnect_event.is_set() or not connected:
            continue

        try:
            master.mav.request_data_stream_send(
                master.target_system, master.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_POSITION, int(hz), 1
            )
            master.mav.request_data_stream_send(
                master.target_system, master.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_EXTENDED_STATUS, 2, 1
            )

            last_send = 0.0
            while not reconnect_event.is_set():
                msg = master.recv_match(type=["GLOBAL_POSITION_INT", "HEARTBEAT", "GPS_RAW_INT"], blocking=False)
                if msg:
                    msg_type = msg.get_type()
                    
                    if msg_type == "HEARTBEAT":
                        if msg.get_srcSystem() == master.target_system and msg.get_srcComponent() == master.target_component:
                            system_status["cube_connected"] = True
                            system_status["cube_status"] = "Connected"
                            system_status["last_hb_time"] = time.time()
                            try:
                                system_status["flight_mode"] = master.flightmode
                            except Exception:
                                pass

                    elif msg_type == "GPS_RAW_INT":
                        system_status["gps_status"] = get_gps_fix_label(getattr(msg, "fix_type", 0))
                        system_status["satellites"] = getattr(msg, "satellites_visible", 0)

                    elif msg_type == "GLOBAL_POSITION_INT":
                        now = time.time()
                        if (now - last_send) >= (1.0 / hz):
                            lat = msg.lat / 1e7
                            lon = msg.lon / 1e7
                            alt = msg.relative_alt / 1000.0
                            heading = msg.hdg / 100.0 if msg.hdg != 65535 else 0.0
                            speed = math.sqrt(msg.vx**2 + msg.vy**2) / 100.0

                            queue_telemetry(v_id, lat, lon, alt, heading, speed)
                            last_send = now

                await asyncio.sleep(0.01)

        except Exception as e:
            print(f"MAVLink polling error: {e}")
            await asyncio.sleep(2.0)

# --- Main Entry Point ---

async def main():
    global config
    config = load_config()

    await start_web_server()

    while True:
        reconnect_event.clear()
        current_config = config.copy()

        while not telemetry_queue.empty():
            telemetry_queue.get_nowait()
            
        mav_task = asyncio.create_task(mavlink_loop(current_config))
        ws_task = asyncio.create_task(ws_loop(current_config))

        # Wait until a settings update triggers a restart
        await reconnect_event.wait()
        
        print("Settings updated via web UI! Terminating connections to reconnect...")
        mav_task.cancel()
        ws_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())