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
    "server_ws_url": "ws://10.24.5.220:8000/ws/vehicle",
    "vehicle_id": "yp",
    "serial_port": "/dev/ttyACM0",
    "baud_rate": 115200,
    "send_hz": 5.0,
    "web_port": 8080,
}

config = {}
reconnect_event = asyncio.Event()

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

# --- Web Server Handler & Simple HTML Page ---

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Raspberry Pi - Telemetry Config</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; padding: 20px; max-width: 500px; margin: 0 auto; }}
        h2 {{ color: #38bdf8; border-bottom: 2px solid #334155; padding-bottom: 10px; }}
        label {{ display: block; margin-top: 15px; font-weight: 600; font-size: 0.9rem; color: #94a3b8; }}
        input, select {{ width: 100%; padding: 10px; margin-top: 5px; border-radius: 6px; border: 1px solid #475569; background: #1e293b; color: white; box-sizing: border-box; font-size: 1rem; }}
        button {{ width: 100%; margin-top: 25px; padding: 12px; background: #2563eb; color: white; border: none; border-radius: 6px; font-size: 1rem; font-weight: bold; cursor: pointer; }}
        button:hover {{ background: #1d4ed8; }}
        .status {{ margin-top: 15px; padding: 10px; background: #065f46; color: #34d399; border-radius: 6px; display: none; text-align: center; }}
    </style>
</head>
<body>
    <h2>Telemetry Transmitter Config</h2>
    <form method="POST" action="/save">
        <label>YP-CS Server WebSocket URL</label>
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

async def handle_save(request):
    data = await request.post()
    global config
    config["server_ws_url"] = data.get("server_ws_url", config["server_ws_url"]).strip()
    config["vehicle_id"] = data.get("vehicle_id", config["vehicle_id"]).strip()
    config["serial_port"] = data.get("serial_port", config["serial_port"]).strip()
    config["baud_rate"] = int(data.get("baud_rate", config["baud_rate"]))
    config["send_hz"] = float(data.get("send_hz", config["send_hz"]))

    save_config(config)
    
    # Trigger reconnection with new config
    reconnect_event.set()

    return web.HTTPFound(location="/")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
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

# --- Telemetry & MAVLink Processing ---

async def send_telemetry(ws: websockets.WebSocketClientProtocol, vehicle_id: str, lat: float, lon: float, alt: float, heading: float, speed: float):
    sec, nanosec, stamp = ros_stamp()
    messages = [
        wrap(vehicle_id, "heartbeat", "yp_ground_station/msg/Heartbeat", stamp, {"mode": "ship-gps", "armed": True}),
        wrap(
            vehicle_id,
            "navsatfix",
            "sensor_msgs/msg/NavSatFix",
            stamp,
            {
                "header": {"stamp": {"sec": sec, "nanosec": nanosec}, "frame_id": "yp_gps"},
                "status": {"status": 0, "service": 1},
                "latitude": lat,
                "longitude": lon,
                "altitude": alt,
                "position_covariance": [0.0] * 9,
                "position_covariance_type": 0,
                "heading": heading,
                "speed_mps": speed,
            },
        ),
        wrap(
            vehicle_id,
            "pose",
            "geometry_msgs/msg/Pose",
            stamp,
            {
                "position": {"x": 0.0, "y": 0.0, "z": alt},
                "orientation": yaw_to_quaternion(heading),
                "heading": heading,
            },
        ),
    ]
    for msg in messages:
        await ws.send(json.dumps(msg))

async def mavlink_loop(ws: websockets.WebSocketClientProtocol, current_config: dict):
    port = current_config["serial_port"]
    baud = current_config["baud_rate"]
    hz = current_config["send_hz"]
    v_id = current_config["vehicle_id"]

    print(f"Connecting to Cube on {port} at {baud} baud...")
    master = mavutil.mavlink_connection(port, baud=baud)
    
    # Non-blocking connection wait that respects reconnect events
    while not reconnect_event.is_set():
        if master.wait_heartbeat(timeout=1.0):
            print("Heartbeat received from Cube!")
            break
        await asyncio.sleep(0.1)

    if reconnect_event.is_set():
        return

    master.mav.request_data_stream_send(
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_POSITION,
        int(hz),
        1,
    )

    last_send = 0.0
    while not reconnect_event.is_set():
        msg = master.recv_match(type="GLOBAL_POSITION_INT", blocking=False)
        if msg:
            now = time.time()
            if (now - last_send) >= (1.0 / hz):
                lat = msg.lat / 1e7
                lon = msg.lon / 1e7
                alt = msg.relative_alt / 1000.0
                heading = msg.hdg / 100.0 if msg.hdg != 65535 else 0.0
                speed = math.sqrt(msg.vx**2 + msg.vy**2) / 100.0

                await send_telemetry(ws, v_id, lat, lon, alt, heading, speed)
                last_send = now

        await asyncio.sleep(0.01)

# --- Main Entry Point ---

async def main():
    global config
    config = load_config()

    # Start embedded web server
    await start_web_server()

    while True:
        reconnect_event.clear()
        current_config = config.copy()
        
        base_url = current_config["server_ws_url"].rstrip("/")
        vehicle_id = current_config["vehicle_id"]
        uri = f"{base_url}/{vehicle_id}"

        print(f"Connecting WebSocket to {uri}...")
        try:
            async with websockets.connect(uri, ping_interval=30, ping_timeout=20) as ws:
                print("WebSocket Connected!")
                
                # Run MAVLink stream until an error occurs or user changes settings
                mav_task = asyncio.create_task(mavlink_loop(ws, current_config))
                reconnect_task = asyncio.create_task(reconnect_event.wait())

                # Wait for either task to finish
                done, pending = await asyncio.wait(
                    [mav_task, reconnect_task], return_when=asyncio.FIRST_COMPLETED
                )
                
                for task in pending:
                    task.cancel()

                if reconnect_event.is_set():
                    print("Settings updated via web UI! Reconnecting...")

        except Exception as exc:
            print(f"Error in connection loop: {exc}")
            print("Retrying in 3 seconds...")
            # Sleep unless settings were updated
            try:
                await asyncio.wait_for(reconnect_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass

if __name__ == "__main__":
    asyncio.run(main())
