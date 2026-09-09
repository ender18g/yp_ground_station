import asyncio
import json
import math
import os
import time
from typing import Any

import websockets
from pymavlink import mavutil

# Configuration
SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://192.168.0.25:8000/ws/vehicle")
VEHICLE_ID = os.getenv("VEHICLE_ID", "yp")
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyACM0")
BAUD_RATE = int(os.getenv("BAUD_RATE", "115200"))
SEND_HZ = float(os.getenv("SEND_HZ", "5"))

def ros_stamp() -> tuple[int, int, float]:
    stamp = time.time()
    sec = int(stamp)
    return sec, int((stamp - sec) * 1_000_000_000), stamp

def yaw_to_quaternion(yaw_deg: float) -> dict[str, float]:
    half = math.radians(yaw_deg) / 2.0
    return {"x": 0.0, "y": 0.0, "z": math.sin(half), "w": math.cos(half)}

def wrap(topic_suffix: str, msg_type: str, stamp: float, msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "vehicle_id": VEHICLE_ID,
        "vehicle_type": "yp",
        "topic": f"/vehicles/{VEHICLE_ID}/{topic_suffix}",
        "type": msg_type,
        "stamp": stamp,
        "msg": msg,
    }

async def send_telemetry(ws: websockets.WebSocketClientProtocol, lat: float, lon: float, alt: float, heading: float, speed: float):
    sec, nanosec, stamp = ros_stamp()
    
    messages = [
        wrap("heartbeat", "yp_ground_station/msg/Heartbeat", stamp, {"mode": "ship-gps", "armed": True}),
        wrap(
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
            "pose",
            "geometry_msgs/msg/Pose",
            stamp,
            {
                "position": {"x": 0.0, "y": 0.0, "z": alt},
                "orientation": yaw_to_quaternion(heading),
                "heading": heading,
            },
        )
    ]
    for msg in messages:
        await ws.send(json.dumps(msg))

async def mavlink_loop(ws: websockets.WebSocketClientProtocol):
    print(f"Connecting to Cube on {SERIAL_PORT} at {BAUD_RATE} baud...")
    master = mavutil.mavlink_connection(SERIAL_PORT, baud=BAUD_RATE)
    
    master.wait_heartbeat()
    print("Heartbeat received! Requesting GLOBAL_POSITION_INT...")
    
    master.mav.request_data_stream_send(
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_POSITION,
        int(SEND_HZ),
        1
    )

    last_send = 0.0
    while True:
        msg = master.recv_match(type='GLOBAL_POSITION_INT', blocking=False)
        if msg:
            now = time.time()
            if (now - last_send) >= (1.0 / SEND_HZ):
                lat = msg.lat / 1e7
                lon = msg.lon / 1e7
                alt = msg.relative_alt / 1000.0 
                heading = msg.hdg / 100.0 if msg.hdg != 65535 else 0.0
                speed = math.sqrt(msg.vx**2 + msg.vy**2) / 100.0
                
                await send_telemetry(ws, lat, lon, alt, heading, speed)
                last_send = now
        
        await asyncio.sleep(0.01)

async def main():
    uri = f"{SERVER_WS_URL.rstrip('/')}/{VEHICLE_ID}"
    while True:
        try:
            async with websockets.connect(uri, ping_interval=30, ping_timeout=20) as ws:
                print(f"YP Cube telemetry connected to {uri}")
                await mavlink_loop(ws)
        except Exception as exc:
            print(f"YP Cube reconnecting after error: {exc}")
            await asyncio.sleep(2.0)

if __name__ == "__main__":
    asyncio.run(main())