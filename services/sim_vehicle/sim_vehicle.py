from __future__ import annotations

import asyncio
import json
import math
import os
import random
import socket
import time
from typing import Any

import websockets


SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://yp-server:8000/ws/vehicle")
VEHICLE_TYPE = os.getenv("VEHICLE_TYPE", "uav").lower()
VEHICLE_ID = os.getenv("VEHICLE_ID") or f"{VEHICLE_TYPE}-{socket.gethostname()[:12]}"
HOME_LAT = float(os.getenv("HOME_LAT", "38.9822"))
HOME_LON = float(os.getenv("HOME_LON", "-76.4819"))
HOME_ALT = float(os.getenv("HOME_ALT", "45.0" if VEHICLE_TYPE == "uav" else "0.0"))
SEND_HZ = float(os.getenv("SEND_HZ", "5"))
SPAWN_JITTER_DEG = float(os.getenv("SPAWN_JITTER_DEG", "0.004"))
TARGET_JITTER_DEG = float(os.getenv("TARGET_JITTER_DEG", "0.006"))

SPEED_MPS = {"uav": 9.0, "usv": 2.8, "ugv": 2.0, "uuv": 1.3}.get(VEHICLE_TYPE, 5.0)


class VehicleSim:
    def __init__(self) -> None:
        jitter = random.uniform(-SPAWN_JITTER_DEG, SPAWN_JITTER_DEG)
        self.lat = HOME_LAT + jitter
        self.lon = HOME_LON + random.uniform(-SPAWN_JITTER_DEG, SPAWN_JITTER_DEG)
        self.alt = HOME_ALT
        self.heading = random.uniform(0, 360)
        self.battery = random.uniform(0.72, 1.0)
        self.target = self.random_target()
        self.mode = "loiter"
        self.local_x = 0.0
        self.local_y = 0.0
        self.last_step = time.time()
        self.mission_waypoints: list[dict[str, float]] = []
        self.mission_complete_pending = False
        self.rtb_follow_heading: float | None = None
        self.rtb_follow_speed_mps: float | None = None

    def random_target(self) -> dict[str, float]:
        return {
            "latitude": HOME_LAT + random.uniform(-TARGET_JITTER_DEG, TARGET_JITTER_DEG),
            "longitude": HOME_LON + random.uniform(-TARGET_JITTER_DEG, TARGET_JITTER_DEG),
            "altitude": HOME_ALT,
        }

    def handle_command(self, command: dict[str, Any]) -> None:
        command_body = command.get("command", command)
        command_type = command_body.get("type")
        if command_type == "rtb":
            self.mode = "rtb"
            self.mission_waypoints = []
            self.target = {"latitude": HOME_LAT, "longitude": HOME_LON, "altitude": HOME_ALT}
        elif command_type == "rtb_follow":
            self.mode = "rtb_follow"
            self.mission_waypoints = []
            target = command_body.get("target", {})
            self.target = {
                "latitude": float(target.get("latitude", self.lat)),
                "longitude": float(target.get("longitude", self.lon)),
                "altitude": float(target.get("altitude", self.alt)),
            }
            self.rtb_follow_heading = float(command_body.get("heading", self.heading)) % 360.0
            self.rtb_follow_speed_mps = max(0.0, float(command_body.get("speed_mps", SPEED_MPS)))
        elif command_type == "waypoint":
            self.mode = "waypoint"
            self.mission_waypoints = []
            self.rtb_follow_heading = None
            self.rtb_follow_speed_mps = None
            target = command_body.get("target", {})
            self.target = {
                "latitude": float(target.get("latitude", self.lat)),
                "longitude": float(target.get("longitude", self.lon)),
                "altitude": float(target.get("altitude", self.alt)),
            }
        elif command_type == "trajectory":
            self.mode = "trajectory"
            self.rtb_follow_heading = None
            self.rtb_follow_speed_mps = None
        elif command_type in ("search_grid", "mob"):
            self.rtb_follow_heading = None
            self.rtb_follow_speed_mps = None
            # Server embeds pre-computed waypoints as [[lat, lon, alt], ...]
            sim_wps = command_body.get("sim_waypoints", [])
            if sim_wps:
                self.mission_waypoints = [
                    {"latitude": float(wp[0]), "longitude": float(wp[1]), "altitude": float(wp[2])}
                    for wp in sim_wps
                ]
                self.mode = "sar_mission"
                self.target = self.mission_waypoints.pop(0)
        elif command_type == "mission_plan":
            self.rtb_follow_heading = None
            self.rtb_follow_speed_mps = None
            mission_wps = command_body.get("waypoints", [])
            parsed = []
            for wp in mission_wps:
                if not isinstance(wp, dict):
                    continue
                lat = wp.get("latitude")
                lon = wp.get("longitude")
                if lat is None or lon is None:
                    continue
                parsed.append(
                    {
                        "latitude": float(lat),
                        "longitude": float(lon),
                        "altitude": float(wp.get("altitude", HOME_ALT)),
                    }
                )
            if parsed:
                self.mission_complete_pending = False
                self.mission_waypoints = parsed
                self.mode = "mission_plan"
                self.target = self.mission_waypoints.pop(0)

    def step(self) -> None:
        now = time.time()
        dt = min(0.5, max(0.001, now - self.last_step))
        self.last_step = now

        distance = haversine_m(self.lat, self.lon, self.target["latitude"], self.target["longitude"])
        if distance < max(4.0, SPEED_MPS * dt * 2.0):
            if self.mode == "rtb":
                self.mode = "hold"
            elif self.mode == "rtb_follow":
                pass
            elif self.mode in ("sar_mission", "mission_plan"):
                if self.mission_waypoints:
                    self.target = self.mission_waypoints.pop(0)
                else:
                    if self.mode == "mission_plan":
                        self.mission_complete_pending = True
                    self.mode = "loiter"
                    self.target = self.random_target()
            else:
                self.target = self.random_target()
                self.mode = "loiter"
            return

        if self.mode == "rtb_follow" and self.rtb_follow_heading is not None:
            # Station keeping combines the YP velocity with a small position
            # correction. It never caps travel at the moving target, avoiding
            # the overshoot-and-correct oscillation caused by point chasing.
            target_heading = self.rtb_follow_heading
            target_speed = self.rtb_follow_speed_mps or 0.0
            target_bearing = bearing_deg(self.lat, self.lon, self.target["latitude"], self.target["longitude"])
            correction_speed = min(2.0, distance * 0.12)
            correction_bearing = target_bearing
            correction_north = math.cos(math.radians(correction_bearing)) * correction_speed
            correction_east = math.sin(math.radians(correction_bearing)) * correction_speed
            base_north = target_speed * math.cos(math.radians(target_heading))
            base_east = target_speed * math.sin(math.radians(target_heading))
            desired_north = base_north + correction_north
            desired_east = base_east + correction_east
            desired_speed = math.hypot(desired_north, desired_east)
            if desired_speed > 0.01:
                desired_heading = math.degrees(math.atan2(desired_east, desired_north)) % 360.0
                self.heading = smooth_angle(self.heading, desired_heading, min(1.0, dt * 2.5))
            travel = min(desired_speed, SPEED_MPS * 1.5) * dt
        else:
            bearing = bearing_deg(self.lat, self.lon, self.target["latitude"], self.target["longitude"])
            self.heading = smooth_angle(self.heading, bearing, min(1.0, dt * 1.8))
            travel = min(distance, SPEED_MPS * dt)
        self.lat, self.lon = destination_point(self.lat, self.lon, self.heading, travel)

        desired_alt = self.target.get("altitude", HOME_ALT)
        self.alt += max(-1.0, min(1.0, desired_alt - self.alt)) * min(1.0, dt)
        if VEHICLE_TYPE == "usv":
            self.alt = 0.0
        elif VEHICLE_TYPE == "uuv":
            self.alt = min(-1.0, self.alt)

        self.local_x += math.sin(math.radians(self.heading)) * travel
        self.local_y += math.cos(math.radians(self.heading)) * travel
        self.battery = max(0.0, self.battery - dt * 0.000035)

    def messages(self) -> list[dict[str, Any]]:
        sec, nanosec, stamp_float = ros_stamp()
        frame = f"{VEHICLE_ID}/base_link"
        quat = yaw_to_quaternion(self.heading)
        target_transform = {
            "translation": {"x": self.local_x, "y": self.local_y, "z": self.alt},
            "rotation": quat,
        }
        messages = [
            wrap("heartbeat", "yp_ground_station/msg/Heartbeat", stamp_float, {"mode": self.mode, "armed": True}),
            wrap(
                "navsatfix",
                "sensor_msgs/msg/NavSatFix",
                stamp_float,
                {
                    "header": {"stamp": {"sec": sec, "nanosec": nanosec}, "frame_id": "map"},
                    "status": {"status": 0, "service": 1},
                    "latitude": self.lat,
                    "longitude": self.lon,
                    "altitude": self.alt,
                    "position_covariance": [0.0] * 9,
                    "position_covariance_type": 0,
                    "heading": self.heading,
                },
            ),
            wrap(
                "pose",
                "geometry_msgs/msg/Pose",
                stamp_float,
                {
                    "position": {"x": self.local_x, "y": self.local_y, "z": self.alt},
                    "orientation": quat,
                    "heading": self.heading,
                },
            ),
            wrap(
                "battery",
                "sensor_msgs/msg/BatteryState",
                stamp_float,
                {
                    "header": {"stamp": {"sec": sec, "nanosec": nanosec}, "frame_id": frame},
                    "voltage": 22.2 * self.battery,
                    "current": -4.0,
                    "charge": float("nan"),
                    "capacity": float("nan"),
                    "design_capacity": float("nan"),
                    "percentage": self.battery,
                    "power_supply_status": 2,
                    "power_supply_health": 1,
                    "power_supply_technology": 3,
                    "present": True,
                    "cell_voltage": [],
                    "cell_temperature": [],
                    "location": "",
                    "serial_number": VEHICLE_ID,
                },
            ),
            wrap(
                "trajectory",
                "trajectory_msgs/msg/MultiDOFJointTrajectory",
                stamp_float,
                {
                    "header": {"stamp": {"sec": sec, "nanosec": nanosec}, "frame_id": "map"},
                    "joint_names": [frame],
                    "points": [
                        {
                            "transforms": [target_transform],
                            "velocities": [
                                {
                                    "linear": {"x": SPEED_MPS, "y": 0.0, "z": 0.0},
                                    "angular": {"x": 0.0, "y": 0.0, "z": 0.0},
                                }
                            ],
                            "accelerations": [],
                            "time_from_start": {"sec": 2, "nanosec": 0},
                        }
                    ],
                },
            ),
        ]
        if self.mission_complete_pending:
            self.mission_complete_pending = False
            messages.append(wrap("mission", "yp_ground_station/MissionComplete", stamp_float, {"status": "complete"}))
        return messages


def wrap(topic_suffix: str, msg_type: str, stamp: float, msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "vehicle_id": VEHICLE_ID,
        "vehicle_type": VEHICLE_TYPE,
        "topic": f"/vehicles/{VEHICLE_ID}/{topic_suffix}",
        "type": msg_type,
        "stamp": stamp,
        "msg": scrub_nan(msg),
    }


async def main() -> None:
    sim = VehicleSim()
    uri = f"{SERVER_WS_URL.rstrip('/')}/{VEHICLE_ID}"
    while True:
        try:
            async with websockets.connect(uri, ping_interval=30, ping_timeout=20) as ws:
                print(f"{VEHICLE_ID} connected to {uri}")
                sim.last_step = time.time()
                receiver = asyncio.create_task(receive_commands(ws, sim))
                try:
                    while True:
                        sim.step()
                        for msg in sim.messages():
                            await ws.send(json.dumps(msg))
                        await asyncio.sleep(1.0 / SEND_HZ)
                finally:
                    receiver.cancel()
        except Exception as exc:
            print(f"{VEHICLE_ID} reconnecting after error: {exc}")
            await asyncio.sleep(2.0)


async def receive_commands(ws: websockets.WebSocketClientProtocol, sim: VehicleSim) -> None:
    async for raw in ws:
        try:
            sim.handle_command(json.loads(raw))
        except Exception as exc:
            print(f"command parse failed: {exc}")


def ros_stamp() -> tuple[int, int, float]:
    stamp = time.time()
    sec = int(stamp)
    return sec, int((stamp - sec) * 1_000_000_000), stamp


def yaw_to_quaternion(yaw_deg: float) -> dict[str, float]:
    half = math.radians(yaw_deg) / 2.0
    return {"x": 0.0, "y": 0.0, "z": math.sin(half), "w": math.cos(half)}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def destination_point(lat: float, lon: float, bearing: float, distance_m: float) -> tuple[float, float]:
    radius = 6_371_000.0
    brng = math.radians(bearing)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    dr = distance_m / radius
    p2 = math.asin(math.sin(p1) * math.cos(dr) + math.cos(p1) * math.sin(dr) * math.cos(brng))
    l2 = l1 + math.atan2(math.sin(brng) * math.sin(dr) * math.cos(p1), math.cos(dr) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def smooth_angle(current: float, target: float, amount: float) -> float:
    delta = (target - current + 540) % 360 - 180
    return (current + delta * amount) % 360


def scrub_nan(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: scrub_nan(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_nan(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


if __name__ == "__main__":
    asyncio.run(main())
