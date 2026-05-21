import asyncio
import json
import os
import time
import traceback

from pymavlink import mavutil
import websockets

VEHICLE_ID = os.getenv("VEHICLE_ID", "arducopter-uav")
VEHICLE_TYPE = os.getenv("VEHICLE_TYPE", "uav")
#SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://yp-server:8000/ws/vehicle")
SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://localhost:8000/ws/vehicle") # use this when running outside of docker
MAVLINK_URL = os.getenv("MAVLINK_URL", "udpin:0.0.0.0:14551")
SEND_HZ = float(os.getenv("SEND_HZ", "5"))


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
            while True:
                msg = master.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=5)
                if msg is None:
                    print("[WARNING] No GLOBAL_POSITION_INT received")
                    continue

                counter += 1
                lat = msg.lat / 1e7
                lon = msg.lon / 1e7
                alt = msg.relative_alt / 1000.0
                heading = getattr(msg, "hdg", None)
                if heading is not None:
                    heading = heading / 100.0

                payload = create_navsatfix_message(lat, lon, alt, heading)
                json_payload = json.dumps(payload)

                print(f"[INFO] Sending websocket message #{counter}...")
                await ws.send(json_payload)
                print("[SUCCESS] Websocket message sent")

                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=0.1)
                    print("[SERVER RESPONSE]", response)
                    
                    # Parse the response and trigger the waypoint command
                    try:
                        server_msg = json.loads(response)
                        
                        # Verify message intent and target vehicle
                        if (server_msg.get("op") == "command" and 
                            server_msg.get("vehicle_id") == VEHICLE_ID):
                            
                            command_data = server_msg.get("command", {})
                            if command_data.get("type") == "waypoint":
                                target = command_data.get("target", {})
                                
                                target_lat = target.get("latitude")
                                target_lon = target.get("longitude")
                                target_alt = target.get("altitude")
                                
                                # Ensure all coordinate data is present
                                if None not in (target_lat, target_lon, target_alt):
                                    goto_waypoint(master, target_lat, target_lon, target_alt)
                                    print("[SUCCESS] Waypoint command routed to vehicle.")
                                else:
                                    print("[ERROR] Missing coordinates in waypoint command payload.")
                    
                    except json.JSONDecodeError:
                        print("[WARNING] Server response was not valid JSON.")

                except asyncio.TimeoutError:
                    pass

                await asyncio.sleep(1.0 / SEND_HZ)

    except Exception as exc:
        print("\n[ERROR] Websocket connection failed!")
        print(str(exc))
        traceback.print_exc()


if __name__ == "__main__":
    try:
        asyncio.run(telemetry_loop())
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down cleanly")
