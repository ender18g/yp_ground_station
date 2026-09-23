import asyncio
import json
import math
import os
import signal
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
shutdown_event = asyncio.Event()
telemetry_queue = asyncio.Queue(maxsize=50)
rtcm_queue = asyncio.Queue(maxsize=200)  # inbound RTCM correction commands awaiting forward to the Cube

# --- Flight Log Download ---
# This rig is a manually-driven Hunter UGV; the Cube is only ever force-armed so its
# dataflash logger runs, never to actuate motors. Disarming on shutdown flushes/closes
# that log so it can be downloaded.
LOG_DIR = Path(os.getenv("LOG_DOWNLOAD_DIR", "flight_logs"))
LOG_REQUEST_TIMEOUT_S = 5.0
LOG_CHUNK_BYTES = 90  # MAVLink LOG_DATA payload size
LOG_CHUNK_MAX_RETRIES = 8
_cube_master = None  # most recent mavutil connection, used by the shutdown disarm/download sequence

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
        .log-row {{ display: flex; gap: 10px; align-items: center; }}
        .log-row select {{ margin-top: 0; }}
        .log-row button {{ width: auto; margin-top: 0; padding: 10px 16px; white-space: nowrap; }}
        .log-empty {{ color: #94a3b8; font-size: 0.9rem; }}
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

    <h2>Flight Logs</h2>
    <div class="diag-card">
        <div class="log-row">
            <select id="log_select"><option value="">Loading...</option></select>
            <button type="button" onclick="downloadSelectedLog()">Download</button>
        </div>
        <div id="log_empty" class="log-empty" style="display: none; margin-top: 10px;">No flight logs found on this device yet. A log is saved here when this program is stopped (Ctrl+C or service stop).</div>
    </div>

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

        function formatBytes(bytes) {{
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        }}

        async function fetchLogs() {{
            try {{
                const res = await fetch('/api/logs');
                const logs = await res.json();
                const select = document.getElementById('log_select');
                const empty = document.getElementById('log_empty');
                const previous = select.value;
                select.innerHTML = '';
                if (logs.length === 0) {{
                    select.innerHTML = '<option value="">No logs available</option>';
                    empty.style.display = 'block';
                    return;
                }}
                empty.style.display = 'none';
                for (const log of logs) {{
                    const option = document.createElement('option');
                    option.value = log.name;
                    const when = new Date(log.modified * 1000).toLocaleString();
                    option.text = `${{log.name}} (${{formatBytes(log.size)}}, ${{when}})`;
                    select.appendChild(option);
                }}
                if (logs.some(log => log.name === previous)) select.value = previous;
            }} catch (e) {{ console.error("Failed fetching logs", e); }}
        }}

        function downloadSelectedLog() {{
            const select = document.getElementById('log_select');
            if (!select.value) return;
            window.location.href = '/logs/' + encodeURIComponent(select.value);
        }}

        setInterval(fetchLogs, 5000);
        fetchLogs();
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

async def handle_logs_api(request):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in LOG_DIR.iterdir():
        if not path.is_file():
            continue
        stat = path.stat()
        entries.append({"name": path.name, "size": stat.st_size, "modified": stat.st_mtime})
    entries.sort(key=lambda entry: entry["modified"], reverse=True)
    return web.json_response(entries)

async def handle_log_download(request):
    name = request.match_info.get("filename", "")
    # Reject anything that isn't a bare filename to prevent path traversal out of LOG_DIR.
    if not name or name != Path(name).name:
        raise web.HTTPBadRequest(text="Invalid filename")
    path = LOG_DIR / name
    if not path.is_file():
        raise web.HTTPNotFound(text="Log file not found")
    return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{name}"'})

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status_api)
    app.router.add_post("/save", handle_save)
    app.router.add_get("/api/logs", handle_logs_api)
    app.router.add_get("/logs/{filename}", handle_log_download)
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

def _force_arm_for_logging(master) -> None:
    """Force-arm the Cube purely so its dataflash logger runs.

    This rig has no motors/props attached (Here3 GPS + Cube used only to track
    a manually-driven vehicle), so pre-arm checks that assume a flyable
    vehicle (EKF origin, GPS-as-primary-nav, etc.) are disabled and the arm
    command uses ArduPilot's force-arm magic value (21196) to bypass whatever
    checks remain.
    """
    try:
        master.mav.param_set_send(
            master.target_system, master.target_component,
            b"ARMING_CHECK", 0, mavutil.mavlink.MAV_PARAM_TYPE_INT32,
        )
        master.mav.command_long_send(
            master.target_system, master.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0,
            1, 21196, 0, 0, 0, 0, 0,
        )
        print("[ARM] Force-arm sent so the Cube starts logging.")
    except Exception as exc:
        print(f"[ARM] Force-arm request failed: {exc}")

def _download_latest_dataflash_log(master, vehicle_id: str) -> None:
    """Fetch the flight controller's most recently closed dataflash log over MAVLink."""
    try:
        master.mav.log_request_list_send(master.target_system, master.target_component, 0, 0xFFFF)
        log_id = None
        log_size = None
        deadline = time.time() + LOG_REQUEST_TIMEOUT_S
        while time.time() < deadline:
            entry = master.recv_match(type="LOG_ENTRY", blocking=True, timeout=1.0)
            if entry is None:
                continue
            if entry.num_logs == 0 or entry.last_log_num == 0:
                break
            if entry.id == entry.last_log_num:
                log_id, log_size = entry.id, entry.size
                break

        if not log_id or not log_size:
            print(f"[LOG] No dataflash log available on {vehicle_id} to download.")
            return

        data = bytearray(log_size)
        offset = 0
        retries = 0
        while offset < log_size:
            count = min(LOG_CHUNK_BYTES, log_size - offset)
            master.mav.log_request_data_send(master.target_system, master.target_component, log_id, offset, count)
            chunk = master.recv_match(type="LOG_DATA", blocking=True, timeout=2.0)
            if chunk is None or chunk.id != log_id or chunk.ofs != offset:
                retries += 1
                if retries > LOG_CHUNK_MAX_RETRIES:
                    print(f"[LOG] Download of log {log_id} for {vehicle_id} timed out at offset {offset}/{log_size}")
                    return
                continue
            retries = 0
            chunk_len = min(chunk.count, log_size - offset)
            data[offset:offset + chunk_len] = bytes(chunk.data[:chunk_len])
            offset += chunk_len

        master.mav.log_request_end_send(master.target_system, master.target_component)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{vehicle_id}_log{log_id}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.bin"
        (LOG_DIR / filename).write_bytes(bytes(data))
        print(f"[LOG] Saved flight log on shutdown: {filename} ({log_size} bytes)")
    except Exception as exc:
        print(f"[LOG] Shutdown log download failed for {vehicle_id}: {exc}")

async def _disarm_and_download_log(vehicle_id: str) -> None:
    """Disarm the Cube (closing/flushing its dataflash log) then pull it down over MAVLink."""
    master = _cube_master
    if master is None:
        print("[SHUTDOWN] No active Cube connection; skipping disarm/log download.")
        return
    try:
        master.mav.command_long_send(
            master.target_system, master.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0,
            0, 21196, 0, 0, 0, 0, 0,
        )
        print("[SHUTDOWN] Disarm sent; waiting for the Cube to close its log...")
        await asyncio.sleep(2.0)
        await asyncio.wait_for(asyncio.to_thread(_download_latest_dataflash_log, master, vehicle_id), timeout=30.0)
    except asyncio.TimeoutError:
        print("[SHUTDOWN] Flight log download timed out.")
    except Exception as exc:
        print(f"[SHUTDOWN] Disarm/log download failed: {exc}")

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

async def _ws_send_loop(ws):
    # Continuously pull from the telemetry queue and send
    while True:
        msg = await telemetry_queue.get()
        await ws.send(json.dumps(msg))

async def _ws_recv_loop(ws, vehicle_id: str):
    # Listen for server commands (e.g. RTCM corrections) and hand them off to the mavlink loop
    async for raw in ws:
        try:
            server_msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if server_msg.get("op") != "command" or server_msg.get("vehicle_id") != vehicle_id:
            continue
        command_data = server_msg.get("command", {})
        if command_data.get("type") == "rtcm_data":
            try:
                rtcm_queue.put_nowait(command_data)
            except asyncio.QueueFull:
                pass

async def ws_loop(current_config: dict):
    base_url = current_config["server_ws_url"].rstrip("/")
    vehicle_id = current_config["vehicle_id"]
    uri = f"{base_url}/{vehicle_id}"
    ws = None

    while not reconnect_event.is_set():
        if ws is None:
            system_status["ws_status"] = "Connecting..."
            system_status["ws_connected"] = False
            try:
                ws = await asyncio.wait_for(
                    websockets.connect(uri, ping_interval=30, ping_timeout=20),
                    timeout=2.0,
                )
                system_status["ws_connected"] = True
                system_status["ws_status"] = "Connected"
                print(f"WebSocket Connected to {uri}!")
            except Exception as exc:
                system_status["ws_connected"] = False
                system_status["ws_status"] = "Server Offline (Retrying)"
                print(f"WebSocket connection error: {exc}")
                await asyncio.sleep(5.0)
                continue

        send_task = asyncio.create_task(_ws_send_loop(ws))
        recv_task = asyncio.create_task(_ws_recv_loop(ws, vehicle_id))
        try:
            done, pending = await asyncio.wait({send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
        except Exception as exc:
            system_status["ws_connected"] = False
            system_status["ws_status"] = "Disconnected"
            print(f"WebSocket disconnected: {exc}")
        finally:
            send_task.cancel()
            recv_task.cancel()
            await asyncio.gather(send_task, recv_task, return_exceptions=True)
            await ws.close()
            ws = None

        if not reconnect_event.is_set():
            await asyncio.sleep(5.0)

async def mavlink_loop(current_config: dict):
    global _cube_master
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

        _cube_master = master
        _force_arm_for_logging(master)

        try:
            master.mav.request_data_stream_send(
                master.target_system, master.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_POSITION, int(hz), 1
            )
            master.mav.request_data_stream_send(
                master.target_system, master.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_EXTENDED_STATUS, 2, 1
            )
            master.mav.command_long_send(master.target_system, master.target_component, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0, mavutil.mavlink.MAVLINK_MSG_ID_GPS_RAW_INT, int(1e6 / 2), 0, 0, 0, 0, 0)
            master.mav.command_long_send(master.target_system, master.target_component, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0, mavutil.mavlink.MAVLINK_MSG_ID_GPS2_RAW, int(1e6 / 2), 0, 0, 0, 0, 0)

            last_send = 0.0
            while not reconnect_event.is_set():
                # Forward any pending RTCM correction data received from the server to the Cube
                while not rtcm_queue.empty():
                    command_data = rtcm_queue.get_nowait()
                    flags = command_data.get("flags", 0)
                    data_len = command_data.get("len", 0)
                    raw_data = command_data.get("data", [])
                    if data_len > 0:
                        padded_payload = bytearray(raw_data + [0] * (180 - len(raw_data)))
                        try:
                            master.mav.gps_rtcm_data_send(flags, data_len, padded_payload)
                        except Exception as e:
                            print(f"[RTCM] MAVLink send error: {e}")

                msg = master.recv_match(type=["GLOBAL_POSITION_INT", "HEARTBEAT", "GPS_RAW_INT", "GPS2_RAW"], blocking=False)
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

                    elif msg_type in ("GPS_RAW_INT", "GPS2_RAW"):
                        fix_type = getattr(msg, "fix_type", 0)
                        eph = getattr(msg, "eph", 65535)
                        epv = getattr(msg, "epv", 65535)
                        h_acc = getattr(msg, "h_acc", None)
                        v_acc = getattr(msg, "v_acc", None)
                        satellites_visible = getattr(msg, "satellites_visible", 255)
                        system_status["gps_status"] = get_gps_fix_label(fix_type)
                        system_status["satellites"] = satellites_visible
                        try:
                            telemetry_queue.put_nowait(wrap(
                                v_id, "gps_fix", "mavlink/GPS_RAW_INT", time.time(),
                                {
                                    "fix_type": fix_type,
                                    "fix_type_label": get_gps_fix_label(fix_type),
                                    "satellites_visible": satellites_visible if satellites_visible != 255 else None,
                                    "horizontal_accuracy_m": (h_acc / 1000.0) if h_acc else ((eph / 100.0) if eph != 65535 else None),
                                    "vertical_accuracy_m": (v_acc / 1000.0) if v_acc else ((epv / 100.0) if epv != 65535 else None),
                                },
                            ))
                        except asyncio.QueueFull:
                            pass

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

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    while True:
        reconnect_event.clear()
        current_config = config.copy()

        while not telemetry_queue.empty():
            telemetry_queue.get_nowait()
        while not rtcm_queue.empty():
            rtcm_queue.get_nowait()

        mav_task = asyncio.create_task(mavlink_loop(current_config))
        ws_task = asyncio.create_task(ws_loop(current_config))

        # Wait until a settings update or a shutdown request (Ctrl+C / docker stop) fires
        reconnect_wait = asyncio.create_task(reconnect_event.wait())
        shutdown_wait = asyncio.create_task(shutdown_event.wait())
        await asyncio.wait({reconnect_wait, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED)
        reconnect_wait.cancel()
        shutdown_wait.cancel()

        mav_task.cancel()
        ws_task.cancel()
        await asyncio.gather(mav_task, ws_task, return_exceptions=True)

        if shutdown_event.is_set():
            print("Shutdown requested; disarming the Cube and saving its flight log...")
            await _disarm_and_download_log(current_config["vehicle_id"])
            break

        print("Settings updated via web UI! Terminating connections to reconnect...")

if __name__ == "__main__":
    asyncio.run(main())