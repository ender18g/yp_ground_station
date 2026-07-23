from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from typing import Any

import websockets


SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://yp-server:8000/ws/vehicle")
VEHICLE_ID = os.getenv("VEHICLE_ID", "sim-umaa")
BASE_LAT = float(os.getenv("LOOPBACK_LAT", "38.989639"))
BASE_LON = float(os.getenv("LOOPBACK_LON", "-76.478643"))
BASE_ALT = float(os.getenv("LOOPBACK_ALT", "0.0"))


def destination_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    radius_m = 6_378_137.0
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    bearing_rad = math.radians(bearing_deg)
    angular = distance_m / radius_m
    lat2 = math.asin(
        math.sin(lat_rad) * math.cos(angular)
        + math.cos(lat_rad) * math.sin(angular) * math.cos(bearing_rad)
    )
    lon2 = lon_rad + math.atan2(
        math.sin(bearing_rad) * math.sin(angular) * math.cos(lat_rad),
        math.cos(angular) - math.sin(lat_rad) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def command_message(command_type: str, target: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": command_type}
    if target is not None:
        payload["target"] = target
    return {"op": "command", "vehicle_id": VEHICLE_ID, "command": payload}


async def print_telemetry(ws: websockets.WebSocketClientProtocol, duration_s: float) -> None:
    deadline = time.time() + duration_s
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
        except asyncio.TimeoutError:
            continue

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if payload.get("vehicle_id") != VEHICLE_ID:
            continue

        topic = payload.get("topic", "")
        msg = payload.get("msg", {})
        if topic.endswith("/navsatfix"):
            lat = msg.get("latitude")
            lon = msg.get("longitude")
            alt = msg.get("altitude")
            heading = msg.get("heading")
            print(f"NAV lat={lat:.7f} lon={lon:.7f} alt={alt:.2f} heading={heading:.1f}")
        elif topic.endswith("/battery"):
            print(f"BATTERY pct={float(msg.get('percentage', 0.0)):.3f}")
        elif topic.endswith("/heartbeat"):
            print(f"HEARTBEAT mode={msg.get('mode')} armed={msg.get('armed')}")
        elif topic.endswith("/status"):
            print(f"STATUS {msg}")


async def run(args: argparse.Namespace) -> None:
    uri = f"{args.ws_url.rstrip('/')}/{args.vehicle_id}"
    waypoint_lat, waypoint_lon = destination_point(BASE_LAT, BASE_LON, args.waypoint_bearing_deg, args.waypoint_distance_m)
    waypoint = {
        "latitude": waypoint_lat,
        "longitude": waypoint_lon,
        "altitude": args.waypoint_altitude_m,
    }

    async with websockets.connect(uri, ping_interval=10, ping_timeout=10) as ws:
        print(f"Connected to {uri}")
        print(f"Base location: {BASE_LAT:.7f}, {BASE_LON:.7f}")
        print(f"Sending waypoint: {waypoint}")

        await ws.send(json.dumps(command_message("waypoint", waypoint)))
        await print_telemetry(ws, args.waypoint_wait_s)

        print("Sending RTB")
        await ws.send(json.dumps(command_message("rtb")))
        await print_telemetry(ws, args.rtb_wait_s)

        if args.cancel_after_s is not None:
            print("Sending cancel_sar")
            await ws.send(json.dumps(command_message("cancel_sar")))
            await print_telemetry(ws, args.cancel_wait_s)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test the simulated UMAA bridge")
    parser.add_argument("--ws-url", default=SERVER_WS_URL)
    parser.add_argument("--vehicle-id", default=VEHICLE_ID)
    parser.add_argument("--waypoint-distance-m", type=float, default=25.0)
    parser.add_argument("--waypoint-bearing-deg", type=float, default=90.0)
    parser.add_argument("--waypoint-altitude-m", type=float, default=BASE_ALT)
    parser.add_argument("--waypoint-wait-s", type=float, default=10.0)
    parser.add_argument("--rtb-wait-s", type=float, default=10.0)
    parser.add_argument("--cancel-after-s", type=float, default=None)
    parser.add_argument("--cancel-wait-s", type=float, default=5.0)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))