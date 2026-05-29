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
HOME_LAT = float(os.getenv("HOME_LAT", "38.984764"))
HOME_LON = float(os.getenv("HOME_LON", "-76.478643"))
HOME_ALT = float(os.getenv("HOME_ALT", "2.0"))
HEADING_DEG = float(os.getenv("HEADING_DEG", "330"))
SPEED_KNOTS = float(os.getenv("SPEED_KNOTS", "3"))
SEND_HZ = float(os.getenv("SEND_HZ", "5"))
KNOTS_TO_MPS = 0.514444
CIRCLE_LEFT_LON = float(os.getenv("CIRCLE_LEFT_LON", "-76.487031"))
CIRCLE_RIGHT_LON = float(os.getenv("CIRCLE_RIGHT_LON", "-76.479393"))
CIRCLE_CW = os.getenv("CIRCLE_CW", "true").lower() != "false"


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
    # Derive circle geometry from lon boundaries; HOME_LAT is the center latitude.
    center_lon = (CIRCLE_LEFT_LON + CIRCLE_RIGHT_LON) / 2.0
    radius_lon_deg = (CIRCLE_RIGHT_LON - CIRCLE_LEFT_LON) / 2.0
    meters_per_deg_lon = 111320.0 * math.cos(math.radians(HOME_LAT))
    radius_m = radius_lon_deg * meters_per_deg_lon
    speed_mps = SPEED_KNOTS * KNOTS_TO_MPS
    # Degrees of heading change per second to trace the target radius.
    turn_rate_deg_per_s = math.degrees(speed_mps / radius_m)

    # Start at the east boundary, center latitude.
    # CW from that position means heading south (180°); CCW means north (0°).
    lat = HOME_LAT
    lon = CIRCLE_RIGHT_LON
    heading = 180.0 if CIRCLE_CW else 0.0
    last_step = time.time()

    while True:
        now = time.time()
        dt = min(0.5, max(0.001, now - last_step))
        last_step = now
        delta = turn_rate_deg_per_s * dt
        heading = (heading + (delta if CIRCLE_CW else -delta)) % 360
        lat, lon = destination_point(lat, lon, heading, speed_mps * dt)
        await send_fix(ws, lat, lon, HOME_ALT, heading)
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


def destination_point(lat: float, lon: float, bearing: float, distance_m: float) -> tuple[float, float]:
    radius = 6_371_000.0
    brng = math.radians(bearing)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    dr = distance_m / radius
    p2 = math.asin(math.sin(p1) * math.cos(dr) + math.cos(p1) * math.sin(dr) * math.cos(brng))
    l2 = l1 + math.atan2(math.sin(brng) * math.sin(dr) * math.cos(p1), math.cos(dr) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


if __name__ == "__main__":
    asyncio.run(main())
