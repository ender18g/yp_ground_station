import asyncio
import json
import os
import threading
import time
import traceback

from pymavlink import mavutil
import websockets

import sar_missions

VEHICLE_ID = os.getenv("VEHICLE_ID", "arducopter-uav")
VEHICLE_TYPE = os.getenv("VEHICLE_TYPE", "uav")
#SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://yp-server:8000/ws/vehicle")
SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://localhost:8000/ws/vehicle") # use this when running outside of docker
MAVLINK_URL = os.getenv("MAVLINK_URL", "udpin:0.0.0.0:14551")
SEND_HZ = float(os.getenv("SEND_HZ", "5"))

# SAR defaults (override via env vars in docker-compose)
SAR_TAKEOFF_ALT_M = float(os.getenv("SAR_TAKEOFF_ALT_M", "30.0"))
SAR_CLIMB_SPEED_MS = float(os.getenv("SAR_CLIMB_SPEED_MS", "8.0"))
# Set to "false" for surface vehicles (USV/UGV) that don't take off
SAR_INCLUDE_TAKEOFF = os.getenv("SAR_INCLUDE_TAKEOFF", "true").lower() != "false"

# Held by SAR mission threads so the telemetry loop skips MAVLink reads during
# blocking mission upload / arm / start sequences.
_sar_mission_lock = threading.Lock()


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

def goto_waypoint(master, target_lat, target_lon, target_alt, timeout=30):
    """Send vehicle to a waypoint and wait for arrival"""
    print(f"\n[NAV] Flying to waypoint: {target_lat:.6f}, {target_lon:.6f}, alt={target_alt}m")
    
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
    print("\n==============================", flush=True)
    print(" ARDUCOPTER MAVLINK BRIDGE ", flush=True)
    print("==============================\n", flush=True)

    print(f"[INFO] Vehicle ID: {VEHICLE_ID}", flush=True)
    print(f"[INFO] Vehicle Type: {VEHICLE_TYPE}", flush=True)
    print(f"[INFO] MAVLink URL: {MAVLINK_URL}", flush=True)
    print(f"[INFO] WebSocket URL: {SERVER_WS_URL.rstrip('/')}/{VEHICLE_ID}", flush=True)

    print("\n[INFO] Connecting to MAVLink...")
    master = mavutil.mavlink_connection(MAVLINK_URL)

    print("[INFO] Waiting for heartbeat...")
    master.wait_heartbeat()

    print("[SUCCESS] Heartbeat received!")
    print(f"[INFO] Target System: {master.target_system}")
    print(f"[INFO] Target Component: {master.target_component}")

    print("\n[INFO] Requesting position telemetry stream...")
    master.mav.request_data_stream_send(
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_POSITION,
        int(SEND_HZ),
        1,
    )
    print("[SUCCESS] Telemetry stream requested")

    try:
        async with websockets.connect(f"{SERVER_WS_URL.rstrip('/')}/{VEHICLE_ID}", ping_interval=10, ping_timeout=10) as ws:
            print("[SUCCESS] WebSocket connected!")
            counter = 0
            last_send_time = time.time()
            
            while True:
                # 1. Listen for incoming WebSocket commands (short timeout to avoid blocking)
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=0.01)
                    print("[SERVER RESPONSE]", response)
                    
                    try:
                        server_msg = json.loads(response)
                        
                        if (server_msg.get("op") == "command" and 
                            server_msg.get("vehicle_id") == VEHICLE_ID):
                            
                            command_data = server_msg.get("command", {})
                            cmd_type = command_data.get("type")

                        if cmd_type == "waypoint":
                                target = command_data.get("target", {})
                                
                                target_lat = target.get("latitude")
                                target_lon = target.get("longitude")
                                target_alt = target.get("altitude")
                                
                                if None not in (target_lat, target_lon, target_alt):
                                    goto_waypoint(master, target_lat, target_lon, target_alt)
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
                    
                    except json.JSONDecodeError:
                        print("[WARNING] Server response was not valid JSON.")

                except asyncio.TimeoutError:
                    pass # Normal behavior, no incoming command this cycle

                # 2. Check for MAVLink telemetry (non-blocking).
                # Skip MAVLink reads while a SAR mission thread holds the lock to avoid
                # consuming COMMAND_ACKs that the mission sequence is waiting for.
                msg = None
                if not _sar_mission_lock.locked():
                    msg = master.recv_match(type="GLOBAL_POSITION_INT", blocking=False)
                
                # 3. Process and send telemetry at the specified SEND_HZ rate
                now = time.time()
                if msg is not None and (now - last_send_time) >= (1.0 / SEND_HZ):
                    counter += 1
                    lat = msg.lat / 1e7
                    lon = msg.lon / 1e7
                    alt = msg.relative_alt / 1000.0
                    heading = getattr(msg, "hdg", None)
                    if heading is not None:
                        heading = heading / 100.0

                    payload = create_navsatfix_message(lat, lon, alt, heading)
                    json_payload = json.dumps(payload)

                    await ws.send(json_payload)
                    print(f"[INFO] Sent websocket message #{counter}")
                    
                    # Reset the timer
                    last_send_time = now

                # 4. Briefly yield control back to the asyncio event loop
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
    """Blocking: generate, upload, arm, and start a search grid mission."""
    with _sar_mission_lock:
        try:
            ok = sar_missions.execute_search_grid(
                master, lat, lon, grid_size_m, swath_m, altitude_m,
                include_takeoff=SAR_INCLUDE_TAKEOFF,
                takeoff_altitude_m=SAR_TAKEOFF_ALT_M,
                climb_speed_ms=SAR_CLIMB_SPEED_MS,
            )
            print(f"[SAR] Search grid mission {'STARTED' if ok else 'FAILED'}")
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
    """Blocking: generate, upload, arm, and start a MOB curved-track search mission."""
    with _sar_mission_lock:
        try:
            ok = sar_missions.execute_mob_search(
                master, track_points,
                corridor_half_width_m=corridor_half_width_m,
                swath_m=swath_m,
                altitude_m=altitude_m,
                takeoff_altitude_m=takeoff_altitude_m,
                climb_speed_ms=climb_speed_ms,
                include_takeoff=SAR_INCLUDE_TAKEOFF,
            )
            print(f"[SAR] MOB search mission {'STARTED' if ok else 'FAILED'}")
        except Exception as exc:
            print(f"[SAR] MOB search error: {exc}")
            traceback.print_exc()


if __name__ == "__main__":
    try:
        asyncio.run(telemetry_loop())
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down cleanly")
