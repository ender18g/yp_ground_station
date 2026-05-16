from __future__ import annotations

import asyncio
import json
import math
import os
import time
from typing import Any, Optional

import serial
import websockets


SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://yp-server:8000/ws/vehicle")
VEHICLE_ID = os.getenv("VEHICLE_ID", "yp")
GPS_MODE = os.getenv("GPS_MODE", "sim").lower()
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
BAUD_RATE = int(os.getenv("BAUD_RATE", "9600"))
HOME_LAT = float(os.getenv("HOME_LAT", "38.9822"))
HOME_LON = float(os.getenv("HOME_LON", "-76.4819"))
HOME_ALT = float(os.getenv("HOME_ALT", "2.0"))
SEND_HZ = float(os.getenv("SEND_HZ", "5"))


async def main() -> None:
    uri = f"{SERVER_WS_URL.rstrip('/')}/{VEHICLE_ID}"
    while True:
        try:
            async with websockets.connect(uri, ping_interval=10, ping_timeout=10) as ws:
                print(f"YP GPS connected to {uri} in {GPS_MODE} mode")
                if GPS_MODE == "serial":
                    await serial_loop(ws)
                else:
                    await sim_loop(ws)
        except Exception as exc:
            print(f"YP GPS reconnecting after error: {exc}")
            await asyncio.sleep(2.0)


async def sim_loop(ws: websockets.WebSocketClientProtocol) -> None:
    heading = 40.0
    while True:
        await send_fix(ws, HOME_LAT, HOME_LON, HOME_ALT, heading)
        heading = (heading + 0.02) % 360
        await asyncio.sleep(1.0 / SEND_HZ)


async def serial_loop(ws: websockets.WebSocketClientProtocol) -> None:
    with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1) as gps:
        while True:
            line = gps.readline().decode("ascii", errors="ignore").strip()
            parsed = parse_nmea(line)
            if parsed:
                await send_fix(ws, parsed["latitude"], parsed["longitude"], parsed.get("altitude", 0.0), parsed.get("heading", 0.0))
            await asyncio.sleep(0)


async def send_fix(ws: websockets.WebSocketClientProtocol, lat: float, lon: float, alt: float, heading: float) -> None:
    sec, nanosec, stamp = ros_stamp()
    messages = [
        wrap("heartbeat", "yp_ground_station/msg/Heartbeat", stamp, {"mode": "ship-gps", "armed": False}),
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
        ),
        wrap(
            "battery",
            "sensor_msgs/msg/BatteryState",
            stamp,
            {"voltage": 24.0, "current": 0.0, "percentage": 1.0, "present": True},
        ),
    ]
    for msg in messages:
        await ws.send(json.dumps(msg))


def wrap(topic_suffix: str, msg_type: str, stamp: float, msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "vehicle_id": VEHICLE_ID,
        "vehicle_type": "yp",
        "topic": f"/vehicles/{VEHICLE_ID}/{topic_suffix}",
        "type": msg_type,
        "stamp": stamp,
        "msg": msg,
    }


def parse_nmea(line: str) -> Optional[dict[str, float]]:
    if not line.startswith("$"):
        return None
    parts = line.split(",")
    sentence = parts[0][3:]
    if sentence == "GGA" and len(parts) > 9 and parts[2] and parts[4]:
        return {
            "latitude": nmea_coord(parts[2], parts[3]),
            "longitude": nmea_coord(parts[4], parts[5]),
            "altitude": float(parts[9] or 0.0),
        }
    if sentence == "RMC" and len(parts) > 8 and parts[3] and parts[5]:
        return {
            "latitude": nmea_coord(parts[3], parts[4]),
            "longitude": nmea_coord(parts[5], parts[6]),
            "heading": float(parts[8] or 0.0),
        }
    return None


def nmea_coord(raw: str, hemisphere: str) -> float:
    dot = raw.find(".")
    degrees_len = dot - 2
    degrees = float(raw[:degrees_len])
    minutes = float(raw[degrees_len:])
    value = degrees + minutes / 60.0
    if hemisphere in {"S", "W"}:
        value *= -1
    return value


def ros_stamp() -> tuple[int, int, float]:
    stamp = time.time()
    sec = int(stamp)
    return sec, int((stamp - sec) * 1_000_000_000), stamp


def yaw_to_quaternion(yaw_deg: float) -> dict[str, float]:
    half = math.radians(yaw_deg) / 2.0
    return {"x": 0.0, "y": 0.0, "z": math.sin(half), "w": math.cos(half)}


if __name__ == "__main__":
    asyncio.run(main())
