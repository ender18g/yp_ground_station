import asyncio
import json
import math
import os
import socket
import threading
import time
import traceback

from pymavlink import mavutil
import websockets

import sar_missions


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

# --- UPDATED DEFAULTS FOR ARDUCOPTER ---
VEHICLE_ID = os.getenv("VEHICLE_ID", "quadrotorYP") 
VEHICLE_TYPE = os.getenv("VEHICLE_TYPE", "uav")

SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://192.168.0.174:8000/ws/vehicle")
MAVLINK_URL = os.getenv("MAVLINK_URL", "/dev/serial0")   # PiConnect standard serial port
MAVLINK_BAUD = int(os.getenv("MAVLINK_BAUD", "921600"))  # Baud rate parameter
SEND_HZ = float(os.getenv("SEND_HZ", "5")) # rate to send json messages to web frontend
WEBRTC_IP = _resolve_webrtc_ip()

# SAR defaults (override via env vars in docker-compose)
SAR_TAKEOFF_ALT_M = float(os.getenv("SAR_TAKEOFF_ALT_M", "30.0"))
SAR_CLIMB_SPEED_MS = float(os.getenv("SAR_CLIMB_SPEED_MS", "8.0"))
# Set to "false" for surface vehicles (USV/UGV) that don't take off
SAR_INCLUDE_TAKEOFF = os.getenv("SAR_INCLUDE_TAKEOFF", "true").lower() != "false"
# Set to "false" to use the legacy full-mission-upload approach instead of streaming
SAR_STREAMING_MODE = os.getenv("SAR_STREAMING_MODE", "true").lower() != "false"
# Arrival radius used by the streaming carrot-chase loop (metres)
SAR_ARRIVAL_RADIUS_M = float(os.getenv("SAR_ARRIVAL_RADIUS_M", "10.0"))

# Held by SAR mission threads so the telemetry loop skips MAVLink reads during
# blocking mission upload / arm / start sequences.
_sar_mission_lock = threading.Lock()
# Set this event to cancel an in-progress streaming SAR mission.
_sar_stop_event = threading.Event()
_sar_telemetry_lock = threading.Lock()
_sar_latest_nav = {
    "lat": None,
    "lon": None,
    "alt": None,
    "heading": None,
    "stamp": 0.0,
}

SHIP_STATE_TIMEOUT_S = float(os.getenv("SHIP_STATE_TIMEOUT_S", "2.0"))
SHIP_RELATIVE_DEFAULT_UPDATE_HZ = float(os.getenv("SHIP_RELATIVE_UPDATE_HZ", "10.0"))
SHIP_RELATIVE_DEFAULT_ARRIVAL_RADIUS_M = float(os.getenv("SHIP_RELATIVE_ARRIVAL_RADIUS_M", "6.0"))
EARTH_RADIUS_M = 6_378_137.0

_vehicle_state_lock = threading.Lock()
_vehicle_state = {
    "lat": None,
    "lon": None,
    "alt": None,
    "heading_deg": None,
    "stamp": 0.0,
}
_ship_state_lock = threading.Lock()
_ship_state = {
    "vehicle_id": None,
    "lat": None,
    "lon": None,
    "alt": None,
    "heading_deg": None,
    "vn_ms": 0.0,
    "ve_ms": 0.0,
    "stamp": 0.0,
}
_ship_relative_thread: threading.Thread | None = None
_ship_relative_stop_event = threading.Event()


def create_navsatfix_message(lat: float, lon: float, alt: float, heading: float | None = None) -> dict:
    now = time.time()
    sec = int(now)
    nanosec = int((now - sec) * 1e9)

    payload = {
        "vehicle_id": VEHICLE_ID,
        "vehicle_type": VEHICLE_TYPE,
        "topic": f"/vehicles/{VEHICLE_ID}/navsatfix",
        "type": "sensor_msgs/msg/NavSatFix",
        "stamp": now,
        "msg": {
            "header": {
                "stamp": {"sec": sec, "nanosec": nanosec},
                "frame_id": "map",
            },
            "status": {"status": 0, "service": 1},
            "latitude": lat,
            "longitude": lon,
            "altitude": alt,
            "position_covariance": [0.0] * 9,
            "position_covariance_type": 0,
        },
    }

    if heading is not None:
        payload["msg"]["heading"] = heading

    return payload

def create_video_stream_message(webrtc_ip: str) -> dict:
    return {
        "op": "video_stream_update",
        "video": {
            "vehicle_id": VEHICLE_ID,
            "enabled": True,
            "streams": [
                {
                    "label": "Primary WebRTC",
                    "url": f"http://{webrtc_ip}:8889/cam/whep" # TODO!!! Change this to auto match vehicle name
                }
            ]
        }
    }


def _ui_ws_url() -> str:
    base = SERVER_WS_URL.rstrip("/")
    marker = "/ws/vehicle"
    if marker in base:
        return f"{base.split(marker, 1)[0]}/ws/ui"
    return base


def _destination_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    bearing_rad = math.radians(bearing_deg)
    angular = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(
        math.sin(lat_rad) * math.cos(angular)
        + math.cos(lat_rad) * math.sin(angular) * math.cos(bearing_rad)
    )
    lon2 = lon_rad + math.atan2(
        math.sin(bearing_rad) * math.sin(angular) * math.cos(lat_rad),
        math.cos(angular) - math.sin(lat_rad) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def _relative_waypoint_to_global(ship_lat: float, ship_lon: float, ship_heading: float, ship_alt: float, waypoint: dict) -> tuple[float, float, float]:
    local_x = float(waypoint.get("x", 0.0))
    local_y = float(waypoint.get("y", 0.0))
    local_z = float(waypoint.get("z", 0.0))
    distance_m = math.hypot(local_x, local_y)
    relative_bearing_deg = math.degrees(math.atan2(local_x, local_y))
    bearing_deg = (ship_heading + relative_bearing_deg + 360.0) % 360.0
    target_lat, target_lon = _destination_point(ship_lat, ship_lon, bearing_deg, distance_m)
    return target_lat, target_lon, ship_alt + local_z


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = lat2_rad - lat1_rad
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2.0) ** 2
    return EARTH_RADIUS_M * 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _north_east_delta_m(lat_ref: float, lon_ref: float, lat: float, lon: float) -> tuple[float, float]:
    dlat = math.radians(lat - lat_ref)
    dlon = math.radians(lon - lon_ref)
    lat_avg = math.radians((lat_ref + lat) / 2.0)
    north_m = dlat * EARTH_RADIUS_M
    east_m = dlon * EARTH_RADIUS_M * math.cos(lat_avg)
    return north_m, east_m


def _update_vehicle_state(lat: float, lon: float, alt: float, heading: float | None) -> None:
    with _vehicle_state_lock:
        _vehicle_state["lat"] = lat
        _vehicle_state["lon"] = lon
        _vehicle_state["alt"] = alt
        _vehicle_state["heading_deg"] = heading
        _vehicle_state["stamp"] = time.time()


def _capture_sar_telemetry(msg) -> None:
    heading_raw = getattr(msg, "hdg", None)
    heading = (heading_raw / 100.0) if heading_raw is not None and heading_raw != 65535 else None
    with _sar_telemetry_lock:
        _sar_latest_nav["lat"] = msg.lat / 1e7
        _sar_latest_nav["lon"] = msg.lon / 1e7
        _sar_latest_nav["alt"] = msg.relative_alt / 1000.0
        _sar_latest_nav["heading"] = heading
        _sar_latest_nav["stamp"] = time.time()


def _snapshot_sar_telemetry(max_age_s: float = 2.0) -> tuple[float, float, float, float | None] | None:
    with _sar_telemetry_lock:
        stamp = float(_sar_latest_nav.get("stamp") or 0.0)
        lat = _sar_latest_nav.get("lat")
        lon = _sar_latest_nav.get("lon")
        alt = _sar_latest_nav.get("alt")
        heading = _sar_latest_nav.get("heading")
    if lat is None or lon is None or alt is None:
        return None
    if (time.time() - stamp) > max_age_s:
        return None
    return float(lat), float(lon), float(alt), (float(heading) if heading is not None else None)


def _snapshot_vehicle_state() -> dict:
    with _vehicle_state_lock:
        return dict(_vehicle_state)


def _update_ship_state(vehicle: dict) -> None:
    position = vehicle.get("position") or {}
    lat = position.get("latitude")
    lon = position.get("longitude")
    if lat is None or lon is None:
        return

    alt = float(position.get("altitude", 0.0))
    heading = vehicle.get("heading")
    stamp = float(vehicle.get("last_seen") or time.time())
    vehicle_id = vehicle.get("vehicle_id")

    with _ship_state_lock:
        prev_lat = _ship_state.get("lat")
        prev_lon = _ship_state.get("lon")
        prev_stamp = float(_ship_state.get("stamp") or 0.0)
        vn_ms = float(_ship_state.get("vn_ms") or 0.0)
        ve_ms = float(_ship_state.get("ve_ms") or 0.0)
        if prev_lat is not None and prev_lon is not None and stamp > prev_stamp:
            north_m, east_m = _north_east_delta_m(float(prev_lat), float(prev_lon), float(lat), float(lon))
            dt = stamp - prev_stamp
            if dt > 0:
                vn_ms = north_m / dt
                ve_ms = east_m / dt
        _ship_state.update({
            "vehicle_id": vehicle_id,
            "lat": float(lat),
            "lon": float(lon),
            "alt": alt,
            "heading_deg": float(heading) % 360.0 if heading is not None else _ship_state.get("heading_deg"),
            "vn_ms": vn_ms,
            "ve_ms": ve_ms,
            "stamp": stamp,
        })


def _snapshot_ship_state(expected_vehicle_id: str | None = None) -> dict | None:
    with _ship_state_lock:
        if expected_vehicle_id and _ship_state.get("vehicle_id") != expected_vehicle_id:
            return None
        if _ship_state.get("lat") is None or _ship_state.get("lon") is None:
            return None
        return dict(_ship_state)


def _ship_state_is_fresh(ship_state: dict | None) -> bool:
    if not ship_state:
        return False
    return (time.time() - float(ship_state.get("stamp") or 0.0)) <= SHIP_STATE_TIMEOUT_S


async def ship_state_listener_loop() -> None:
    ui_ws_url = _ui_ws_url()
    while True:
        try:
            async with websockets.connect(ui_ws_url, ping_interval=10, ping_timeout=10) as ws:
                print(f"[INFO] Ship-state listener connected to {ui_ws_url}", flush=True)
                async for raw_message in ws:
                    try:
                        message = json.loads(raw_message)
                    except json.JSONDecodeError:
                        continue

                    op = message.get("op")
                    if op == "snapshot":
                        for vehicle in message.get("vehicles", []):
                            if vehicle.get("vehicle_type") == "yp":
                                _update_ship_state(vehicle)
                    elif op == "vehicle_update":
                        vehicle = message.get("vehicle") or {}
                        if vehicle.get("vehicle_type") == "yp":
                            _update_ship_state(vehicle)
        except Exception as exc:
            print(f"[WARN] Ship-state listener disconnected: {exc}", flush=True)
            await asyncio.sleep(1.0)


def _stop_ship_relative_mission() -> None:
    global _ship_relative_thread

    thread = _ship_relative_thread
    if thread and thread.is_alive():
        _ship_relative_stop_event.set()
        thread.join(timeout=1.0)
    _ship_relative_stop_event.clear()
    _ship_relative_thread = None


def _launch_ship_relative_mission(master, command_data: dict) -> None:
    global _ship_relative_thread

    local_waypoints = command_data.get("local_waypoints", [])
    ship_vehicle_id = command_data.get("ship_vehicle_id")
    if not ship_vehicle_id:
        print("[ERROR] ship_relative_trajectory requires ship_vehicle_id.")
        return
    if not local_waypoints:
        print("[ERROR] ship_relative_trajectory requires at least one waypoint.")
        return

    _stop_ship_relative_mission()
    _ship_relative_thread = threading.Thread(
        target=_run_ship_relative_mission,
        args=(master, ship_vehicle_id, local_waypoints, float(command_data.get("arrival_radius_m", SHIP_RELATIVE_DEFAULT_ARRIVAL_RADIUS_M)), float(command_data.get("update_hz", SHIP_RELATIVE_DEFAULT_UPDATE_HZ)), _ship_relative_stop_event),
        daemon=True,
    )
    _ship_relative_thread.start()


def _run_ship_relative_mission(master, ship_vehicle_id: str, local_waypoints: list, arrival_radius_m: float, update_hz: float, stop_event: threading.Event) -> None:
    update_period_s = 1.0 / max(update_hz, 1.0)
    print(f"[SHIP-REL] Starting mission with {len(local_waypoints)} waypoints relative to {ship_vehicle_id}")

    for index, waypoint in enumerate(local_waypoints, start=1):
        while not stop_event.is_set():
            ship_state = _snapshot_ship_state(ship_vehicle_id)
            vehicle_state = _snapshot_vehicle_state()

            if not _ship_state_is_fresh(ship_state):
                print("[SHIP-REL] Waiting for fresh ship state...")
                time.sleep(update_period_s)
                continue

            if ship_state is None:
                time.sleep(update_period_s)
                continue

            if vehicle_state.get("lat") is None or vehicle_state.get("lon") is None or vehicle_state.get("alt") is None:
                print("[SHIP-REL] Waiting for fresh vehicle state...")
                time.sleep(update_period_s)
                continue

            ship_lat = float(ship_state["lat"])
            ship_lon = float(ship_state["lon"])
            ship_heading = float(ship_state.get("heading_deg") or 0.0)
            ship_alt = float(ship_state.get("alt") or 0.0)
            target_lat, target_lon, target_alt = _relative_waypoint_to_global(ship_lat, ship_lon, ship_heading, ship_alt, waypoint)

            # --- OVERRIDE FOR SURFACE VEHICLES ---
            if VEHICLE_TYPE in ["usv", "ugv"]:
                target_alt = 0.0

            master.mav.set_position_target_global_int_send(
                0,
                master.target_system,
                master.target_component,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                int(0b110111000000),
                int(target_lat * 1e7),
                int(target_lon * 1e7),
                target_alt,
                float(ship_state.get("vn_ms") or 0.0),
                float(ship_state.get("ve_ms") or 0.0),
                0.0,
                0,
                0,
                0,
                0,
                0,
            )

            distance_m = _distance_m(float(vehicle_state["lat"]), float(vehicle_state["lon"]), target_lat, target_lon)
           
            # --- OVERRIDE FOR SURFACE VEHICLES ---
            if VEHICLE_TYPE in ["usv", "ugv"]:
                alt_condition_met = True
            else:
                alt_error_m = abs(float(vehicle_state["alt"]) - target_alt)
                alt_condition_met = alt_error_m <= max(2.0, arrival_radius_m * 0.5)

            if distance_m <= arrival_radius_m and alt_condition_met:
                print(f"[SHIP-REL] Reached waypoint {index}/{len(local_waypoints)}")
                break

            time.sleep(update_period_s)

        if stop_event.is_set():
            print("[SHIP-REL] Mission interrupted.")
            return

    print("[SHIP-REL] Mission complete.")


def goto_waypoint(master, target_lat, target_lon, target_alt, timeout=30, force_guided=True):
    """Send vehicle to a waypoint."""
   
    # --- OVERRIDE FOR SURFACE VEHICLES ---
    if VEHICLE_TYPE in ["usv", "ugv"]:
        target_alt = 0.0
       
    print(f"\n[NAV] Flying/Driving to waypoint: {target_lat:.6f}, {target_lon:.6f}, alt={target_alt}m")
    
    if force_guided:
        master.set_mode('GUIDED')
                                
    master.mav.set_position_target_global_int_send(
        0,
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        int(0b110111111000),
        int(target_lat * 1e7),
        int(target_lon * 1e7),
        target_alt,
        0, 0, 0,
        0, 0, 0,
        0, 0
    )


async def telemetry_loop() -> None:
    global VEHICLE_TYPE
    global SAR_INCLUDE_TAKEOFF

    print("\n==============================", flush=True)
    print(" ARDUCOPTER BRIDGE ", flush=True)
    print("==============================\n", flush=True)

    print(f"[INFO] Initial Vehicle ID: {VEHICLE_ID}", flush=True)
    print(f"[INFO] Initial Vehicle Type: {VEHICLE_TYPE}", flush=True)
    print(f"[INFO] MAVLink URL: {MAVLINK_URL}", flush=True)
    print(f"[INFO] MAVLink Baud: {MAVLINK_BAUD}", flush=True)
    print(f"[INFO] WebSocket URL: {SERVER_WS_URL.rstrip('/')}/{VEHICLE_ID}", flush=True)
    print(f"[INFO] WebRTC IP: {WEBRTC_IP}", flush=True)

    print("\n[INFO] Connecting to MAVLink...")
    master = mavutil.mavlink_connection(MAVLINK_URL, baud=MAVLINK_BAUD)

    print("[INFO] Waiting for heartbeat...")
    msg = master.wait_heartbeat()

    print("[SUCCESS] Heartbeat received!")
    print(f"[INFO] Target System: {master.target_system}")
    print(f"[INFO] Target Component: {master.target_component}")
    print(f"[INFO] MAV_TYPE Detected: {msg.type}")

    if msg.type in [10, 22]:
        VEHICLE_TYPE = "usv"
        SAR_INCLUDE_TAKEOFF = False
        print("[INFO] Vehicle identified as Surface/Ground. Adapted logic: Alt=0, No Takeoff.")
   
    print("\n[INFO] Requesting position telemetry stream...")
    master.mav.request_data_stream_send(
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_POSITION,
        int(SEND_HZ),
        1,
    )
    print("[SUCCESS] Telemetry stream requested")

    asyncio.create_task(ship_state_listener_loop())

    try:
        async with websockets.connect(f"{SERVER_WS_URL.rstrip('/')}/{VEHICLE_ID}", ping_interval=10, ping_timeout=10) as ws:
            print("[SUCCESS] WebSocket connected!")
            counter = 0
            last_send_time = time.time()
            last_video_send_time = 0.0
           
            while True:
                # 1. Listen for incoming WebSocket commands
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=0.01)
                    print("[SERVER RESPONSE]", response)
                   
                    try:
                        server_msg = json.loads(response)
                       
                        if (server_msg.get("op") == "command"
                            and server_msg.get("vehicle_id") == VEHICLE_ID):
                            command_data = server_msg.get("command", {})
                            cmd_type = command_data.get("type")

                            if cmd_type == "waypoint":
                                target = command_data.get("target", {})
                                source = server_msg.get("source")
                               
                                target_lat = target.get("latitude")
                                target_lon = target.get("longitude")
                                target_alt = target.get("altitude")

                                if None not in (target_lat, target_lon, target_alt):
                                    goto_waypoint(
                                        master,
                                        target_lat,
                                        target_lon,
                                        target_alt,
                                        force_guided=(source != "rtb_follow"),
                                    )
                                    print("[SUCCESS] Waypoint command routed to vehicle.")
                                else:
                                    print("[ERROR] Missing coordinates in waypoint command payload.")

                            elif cmd_type == "search_grid":
                                lat = command_data.get("lat")
                                lon = command_data.get("lon")
                                grid_size_m = float(command_data.get("grid_size_m", 200))
                                swath_m = float(command_data.get("swath_m", 20))
                                altitude_m = float(command_data.get("altitude_m", 30))
                                if None not in (lat, lon):
                                    print(f"[SAR] Launching search grid at ({lat}, {lon}), {grid_size_m}m grid")
                                    threading.Thread(
                                        target=_run_search_grid,
                                        args=(master, float(lat), float(lon), grid_size_m, swath_m, altitude_m),
                                        daemon=True,
                                    ).start()
                                else:
                                    print("[ERROR] search_grid command missing lat/lon.")

                            elif cmd_type == "mob":
                                track_points = command_data.get("track_points", [])
                                corridor_half_width_m = float(command_data.get("corridor_half_width_m", 50.0))
                                swath_m = float(command_data.get("swath_m", 20.0))
                                altitude_m = float(command_data.get("altitude_m", 30.0))
                                takeoff_altitude_m = float(command_data.get("takeoff_altitude_m", SAR_TAKEOFF_ALT_M))
                                climb_speed_ms = float(command_data.get("climb_speed_ms", SAR_CLIMB_SPEED_MS))
                                if len(track_points) >= 2:
                                    print(f"[SAR] MAN OVERBOARD — launching search on {len(track_points)}-point track")
                                    threading.Thread(
                                        target=_run_mob_search,
                                        args=(master, track_points, corridor_half_width_m, swath_m, altitude_m, takeoff_altitude_m, climb_speed_ms),
                                        daemon=True,
                                    ).start()
                                else:
                                    print(f"[ERROR] MOB command needs at least 2 track points, got {len(track_points)}")

                            elif cmd_type == "cancel_sar":
                                _sar_stop_event.set()
                                print("[SAR] Cancel requested by operator.")
                           
                            elif cmd_type == "ship_relative_trajectory":
                                _launch_ship_relative_mission(master, command_data)
                                print("[INFO] Ship-relative trajectory command launched.")

                            elif cmd_type == "mission_plan":
                                waypoints = command_data.get("waypoints", [])
                                auto_arm_start = bool(command_data.get("auto_arm_start", True))
                                force_guided_on_complete = bool(command_data.get("force_guided_on_complete", False))
                                if isinstance(waypoints, list) and len(waypoints) > 0:
                                    threading.Thread(
                                        target=_run_mission_plan,
                                        args=(master, waypoints, auto_arm_start, force_guided_on_complete),
                                        daemon=True,
                                    ).start()
                                else:
                                    print("[ERROR] mission_plan command missing waypoints array.")
                            elif cmd_type == "set_mode":
                                mode = command_data.get("mode")
                                if mode:
                                    sar_missions.set_mode(master, str(mode), wait_for_ack=False)
                                    print(f"[INFO] Set vehicle mode to {mode}")
                                else:
                                    print("[ERROR] set_mode command missing mode field.")

                    except json.JSONDecodeError:
                        print("[WARNING] Server response was not valid JSON.")

                except asyncio.TimeoutError:
                    pass

                # 2. Check for MAVLink telemetry
                msg = None
                if not _sar_mission_lock.locked():
                    msg = master.recv_match(type="GLOBAL_POSITION_INT", blocking=False)
               
                # 3. Process and send telemetry at SEND_HZ rate
                now = time.time()
                telemetry_sample = None
                if msg is not None:
                    lat = msg.lat / 1e7
                    lon = msg.lon / 1e7
                    alt = msg.relative_alt / 1000.0
                    heading_raw = getattr(msg, "hdg", None)
                    heading = (heading_raw / 100.0) if heading_raw is not None and heading_raw != 65535 else None
                    _update_vehicle_state(lat, lon, alt, heading)
                    telemetry_sample = (lat, lon, alt, heading)
                else:
                    telemetry_sample = _snapshot_sar_telemetry()
                    if telemetry_sample is not None:
                        _update_vehicle_state(*telemetry_sample)

                if telemetry_sample is not None and (now - last_send_time) >= (1.0 / SEND_HZ):
                    counter += 1

                    lat, lon, alt, heading = telemetry_sample

                    payload = create_navsatfix_message(lat, lon, alt, heading)
                    json_payload = json.dumps(payload)

                    await ws.send(json_payload)
                    last_send_time = now

                # 4. Periodically send the WebRTC video stream update heartbeat
                if now - last_video_send_time >= 60.0:
                    video_payload = create_video_stream_message(WEBRTC_IP)
                    await ws.send(json.dumps(video_payload))
                    print(f"[INFO] Sent video_stream_update heartbeat for {WEBRTC_IP}")
                    last_video_send_time = now

                # 5. Briefly yield control back to the asyncio event loop
                await asyncio.sleep(0.01)

    except Exception as exc:
        print("\n[ERROR] Websocket connection failed!")
        print(str(exc))
        traceback.print_exc()


# ---------------------------------------------------------------------------
# SAR mission thread targets
# ---------------------------------------------------------------------------

def _run_search_grid(
    master,
    lat: float, lon: float,
    grid_size_m: float, swath_m: float, altitude_m: float,
) -> None:
    if VEHICLE_TYPE in ["usv", "ugv"]:
        altitude_m = 0.0

    with _sar_mission_lock:
        _sar_stop_event.clear()
        try:
            if not SAR_STREAMING_MODE:
                print("[SAR] SAR_STREAMING_MODE=false ignored; forcing streaming carrot-chase mode.")
            ok = sar_missions.execute_search_grid_streaming(
                master, lat, lon, grid_size_m, swath_m, altitude_m,
                include_takeoff=SAR_INCLUDE_TAKEOFF,
                takeoff_altitude_m=SAR_TAKEOFF_ALT_M,
                climb_speed_ms=SAR_CLIMB_SPEED_MS,
                arrival_radius_m=SAR_ARRIVAL_RADIUS_M,
                stop_event=_sar_stop_event,
                telemetry_callback=_capture_sar_telemetry,
            )
            print(f"[SAR] Search grid mission (streaming) {'COMPLETE' if ok else 'FAILED'}")
        except Exception as exc:
            print(f"[SAR] Search grid error: {exc}")
            traceback.print_exc()


def _run_mob_search(
    master,
    track_points: list,
    corridor_half_width_m: float,
    swath_m: float,
    altitude_m: float,
    takeoff_altitude_m: float,
    climb_speed_ms: float,
) -> None:
    if VEHICLE_TYPE in ["usv", "ugv"]:
        altitude_m = 0.0
        takeoff_altitude_m = 0.0

    with _sar_mission_lock:
        _sar_stop_event.clear()
        try:
            if not SAR_STREAMING_MODE:
                print("[SAR] SAR_STREAMING_MODE=false ignored; forcing streaming carrot-chase mode.")
            ok = sar_missions.execute_mob_search_streaming(
                master, track_points,
                corridor_half_width_m=corridor_half_width_m,
                swath_m=swath_m,
                altitude_m=altitude_m,
                takeoff_altitude_m=takeoff_altitude_m,
                climb_speed_ms=climb_speed_ms,
                include_takeoff=SAR_INCLUDE_TAKEOFF,
                arrival_radius_m=SAR_ARRIVAL_RADIUS_M,
                stop_event=_sar_stop_event,
                telemetry_callback=_capture_sar_telemetry,
            )
            print(f"[SAR] MOB search mission (streaming) {'COMPLETE' if ok else 'FAILED'}")
        except Exception as exc:
            print(f"[SAR] MOB search error: {exc}")
            traceback.print_exc()


def _run_mission_plan(master, waypoints: list, auto_arm_start: bool, force_guided_on_complete: bool) -> None:
    with _sar_mission_lock:
        try:
            item_type_to_cmd = {
                "waypoint": int(mavutil.mavlink.MAV_CMD_NAV_WAYPOINT),
                "takeoff": int(mavutil.mavlink.MAV_CMD_NAV_TAKEOFF),
                "loiter_time": int(mavutil.mavlink.MAV_CMD_NAV_LOITER_TIME),
                "land": int(mavutil.mavlink.MAV_CMD_NAV_LAND),
                "rtl": int(mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH),
                "do_jump": int(mavutil.mavlink.MAV_CMD_DO_JUMP),
            }
            mission_items = []
            for wp in waypoints:
                if not isinstance(wp, dict):
                    continue
                lat = wp.get("latitude")
                lon = wp.get("longitude")
                if lat is None or lon is None:
                    continue

                item_type = str(wp.get("item_type") or "waypoint").lower()
                command_id = int(wp.get("command_id") or item_type_to_cmd.get(item_type, item_type_to_cmd["waypoint"]))

                altitude = float(wp.get("altitude", 30.0))
                if VEHICLE_TYPE in ["usv", "ugv"]:
                    altitude = 0.0

                default_p1 = float(wp.get("hold_time_s", 0.0))
                default_p2 = float(wp.get("acceptance_radius_m", 8.0))
                default_p3 = 0.0
                default_p4 = float(wp.get("yaw_deg", 0.0) or 0.0)

                mission_items.append(
                    (
                        float(lat),
                        float(lon),
                        altitude,
                        command_id,
                        float(wp.get("param1", default_p1)),
                        float(wp.get("param2", default_p2)),
                        float(wp.get("param3", default_p3)),
                        float(wp.get("param4", default_p4)),
                    )
                )

            if not mission_items:
                print("[MISSION] mission_plan has no valid waypoints.")
                return

            if force_guided_on_complete:
                last_lat, last_lon, last_alt = mission_items[-1][0], mission_items[-1][1], mission_items[-1][2]
                mission_items.append(
                    (
                        float(last_lat),
                        float(last_lon),
                        float(last_alt),
                        int(mavutil.mavlink.MAV_CMD_NAV_GUIDED_ENABLE),
                        1.0,
                        0.0,
                        0.0,
                        0.0,
                    )
                )

            print(f"[MISSION] Uploading mission with {len(mission_items)} items")
            if not sar_missions.upload_mission(master, mission_items):
                print("[MISSION] Mission upload failed")
                return

            if auto_arm_start:
                sar_missions.set_mode(master, "AUTO", wait_for_ack=False)
                time.sleep(0.2)
                sar_missions.arm_vehicle(master)
                time.sleep(0.2)
                sar_missions.start_mission(master)
                print("[MISSION] Mission armed and started")
        except Exception as exc:
            print(f"[MISSION] mission_plan error: {exc}")
            traceback.print_exc()


if __name__ == "__main__":
    try:
        asyncio.run(telemetry_loop())
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down cleanly")
