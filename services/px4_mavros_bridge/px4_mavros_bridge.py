from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import websockets


VEHICLE_ID = os.getenv("VEHICLE_ID", "px4-uav")
VEHICLE_TYPE = os.getenv("VEHICLE_TYPE", "uav")
SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://yp-server:8000/ws/vehicle")
ROSBRIDGE_URL = os.getenv("ROSBRIDGE_URL", "ws://rosbridge:9090")
SETPOINT_HZ = float(os.getenv("SETPOINT_HZ", "5"))
AUTO_ARM_OFFBOARD = os.getenv("AUTO_ARM_OFFBOARD", "true").lower() in {"1", "true", "yes", "on"}
GLOBAL_SETPOINT_FRAME = int(os.getenv("GLOBAL_SETPOINT_FRAME", "6"))
DISCOVER_MAVROS_TOPICS = os.getenv("DISCOVER_MAVROS_TOPICS", "true").lower() in {"1", "true", "yes", "on"}

SUBSCRIPTIONS: list[tuple[str, str]] = [
    ("/mavros/state", "mavros_msgs/State"),
    ("/mavros/extended_state", "mavros_msgs/ExtendedState"),
    ("/mavros/global_position/global", "sensor_msgs/NavSatFix"),
    ("/mavros/global_position/compass_hdg", "std_msgs/Float64"),
    ("/mavros/global_position/rel_alt", "std_msgs/Float64"),
    ("/mavros/local_position/pose", "geometry_msgs/PoseStamped"),
    ("/mavros/local_position/velocity_local", "geometry_msgs/TwistStamped"),
    ("/mavros/battery", "sensor_msgs/BatteryState"),
    ("/mavros/imu/data", "sensor_msgs/Imu"),
    ("/mavros/home_position/home", "mavros_msgs/HomePosition"),
    ("/mavros/gpsstatus/gps1/raw", "mavros_msgs/GPSRAW"),
]

SETPOINT_TOPIC = "/mavros/setpoint_raw/global"
SETPOINT_TYPE = "mavros_msgs/GlobalPositionTarget"


class Bridge:
    def __init__(self) -> None:
        self.ros_ws: websockets.WebSocketClientProtocol | None = None
        self.vehicle_ws: websockets.WebSocketClientProtocol | None = None
        self.active_waypoint: dict[str, float] | None = None
        self.last_heading: float | None = None
        self.topic_types = dict(SUBSCRIPTIONS)

    async def run_forever(self) -> None:
        while True:
            try:
                async with websockets.connect(ROSBRIDGE_URL, ping_interval=10, ping_timeout=10) as ros_ws:
                    async with websockets.connect(self.vehicle_uri(), ping_interval=10, ping_timeout=10) as vehicle_ws:
                        self.ros_ws = ros_ws
                        self.vehicle_ws = vehicle_ws
                        print(f"{VEHICLE_ID} bridge connected to rosbridge and YP server")
                        await self.setup_rosbridge()
                        await asyncio.gather(
                            self.read_rosbridge(),
                            self.read_vehicle_commands(),
                            self.publish_setpoints(),
                        )
            except Exception as exc:
                print(f"{VEHICLE_ID} bridge reconnecting after error: {exc}")
                self.ros_ws = None
                self.vehicle_ws = None
                await asyncio.sleep(2.0)

    def vehicle_uri(self) -> str:
        return f"{SERVER_WS_URL.rstrip('/')}/{VEHICLE_ID}"

    async def setup_rosbridge(self) -> None:
        await self.ros_send({"op": "advertise", "topic": SETPOINT_TOPIC, "type": SETPOINT_TYPE})
        self.topic_types = dict(SUBSCRIPTIONS)
        if DISCOVER_MAVROS_TOPICS:
            self.topic_types.update(await self.discover_mavros_topics())
        for topic, msg_type in sorted(self.topic_types.items()):
            await self.ros_send(
                {
                    "op": "subscribe",
                    "topic": topic,
                    "type": msg_type,
                    "throttle_rate": 100,
                    "queue_length": 1,
                }
            )

    async def read_rosbridge(self) -> None:
        assert self.ros_ws
        async for raw in self.ros_ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if payload.get("op") != "publish":
                continue
            topic = str(payload.get("topic", ""))
            msg = payload.get("msg", {})
            msg_type = ros1_to_ros2_type(self.topic_types.get(topic, "unknown"))
            await self.forward(topic, msg_type, msg)
            for alias_topic, alias_type, alias_msg in self.canonical_aliases(topic, msg_type, msg):
                await self.forward(alias_topic, alias_type, alias_msg)

    async def read_vehicle_commands(self) -> None:
        assert self.vehicle_ws
        async for raw in self.vehicle_ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            command = payload.get("command", payload)
            if not isinstance(command, dict):
                continue
            if command.get("type") == "waypoint":
                await self.handle_waypoint(command)
            elif command.get("type") == "rtb":
                await self.call_service("/mavros/set_mode", "mavros_msgs/SetMode", {"base_mode": 0, "custom_mode": "AUTO.RTL"})

    async def handle_waypoint(self, command: dict[str, Any]) -> None:
        target = command.get("target") or {}
        self.active_waypoint = {
            "latitude": float(target["latitude"]),
            "longitude": float(target["longitude"]),
            "altitude": float(target.get("altitude", 45.0)),
        }
        await self.publish_setpoint_once()
        if AUTO_ARM_OFFBOARD:
            asyncio.create_task(self.enter_offboard_after_setpoints())

    async def enter_offboard_after_setpoints(self) -> None:
        await asyncio.sleep(1.2)
        await self.call_service("/mavros/cmd/arming", "mavros_msgs/CommandBool", {"value": True})
        await self.call_service("/mavros/set_mode", "mavros_msgs/SetMode", {"base_mode": 0, "custom_mode": "OFFBOARD"})

    async def publish_setpoints(self) -> None:
        while True:
            await self.publish_setpoint_once()
            await asyncio.sleep(1.0 / SETPOINT_HZ)

    async def publish_setpoint_once(self) -> None:
        if not self.active_waypoint:
            return
        await self.ros_send(
            {
                "op": "publish",
                "topic": SETPOINT_TOPIC,
                "type": SETPOINT_TYPE,
                "msg": global_position_target(self.active_waypoint, self.last_heading),
            }
        )

    async def call_service(self, service: str, service_type: str, args: dict[str, Any]) -> None:
        await self.ros_send(
            {
                "op": "call_service",
                "service": service,
                "type": service_type,
                "args": args,
                "id": f"{VEHICLE_ID}-{service}-{time.time()}",
            }
        )

    async def discover_mavros_topics(self) -> dict[str, str]:
        try:
            values = await self.call_rosapi("/rosapi/topics", {})
            topics = values.get("topics", [])
            discovered: dict[str, str] = {}
            for topic in topics:
                if not isinstance(topic, str) or not topic.startswith("/mavros/"):
                    continue
                type_values = await self.call_rosapi("/rosapi/topic_type", {"topic": topic})
                msg_type = type_values.get("type")
                if isinstance(msg_type, str) and msg_type:
                    discovered[topic] = msg_type
            if discovered:
                print(f"{VEHICLE_ID} discovered {len(discovered)} MAVROS topics")
            return discovered
        except Exception as exc:
            print(f"{VEHICLE_ID} MAVROS topic discovery skipped: {exc}")
            return {}

    async def call_rosapi(self, service: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self.ros_ws:
            return {}
        request_id = f"{VEHICLE_ID}-{service}-{time.time()}"
        await self.ros_ws.send(json.dumps({"op": "call_service", "service": service, "args": args, "id": request_id}))
        deadline = time.time() + 3.0
        while time.time() < deadline:
            raw = await asyncio.wait_for(self.ros_ws.recv(), timeout=max(0.1, deadline - time.time()))
            payload = json.loads(raw)
            if payload.get("op") == "service_response" and payload.get("id") == request_id:
                values = payload.get("values", {})
                return values if isinstance(values, dict) else {}
        return {}

    async def forward(self, topic: str, msg_type: str, msg: Any) -> None:
        if not self.vehicle_ws:
            return
        await self.vehicle_ws.send(
            json.dumps(
                {
                    "vehicle_id": VEHICLE_ID,
                    "vehicle_type": VEHICLE_TYPE,
                    "topic": topic_for_vehicle(topic),
                    "type": msg_type,
                    "stamp": time.time(),
                    "msg": msg,
                }
            )
        )

    def canonical_aliases(self, topic: str, msg_type: str, msg: Any) -> list[tuple[str, str, dict[str, Any]]]:
        if not isinstance(msg, dict):
            return []
        if topic == "/mavros/global_position/global":
            return [("navsatfix", "sensor_msgs/msg/NavSatFix", msg)]
        if topic == "/mavros/local_position/pose":
            pose = msg.get("pose")
            if isinstance(pose, dict):
                return [("pose", "geometry_msgs/msg/Pose", pose)]
        if topic == "/mavros/battery":
            return [("battery", "sensor_msgs/msg/BatteryState", msg)]
        if topic == "/mavros/global_position/compass_hdg":
            heading = msg.get("data")
            if isinstance(heading, (int, float)):
                self.last_heading = float(heading) % 360
                return [("heading", "yp_ground_station/msg/Heading", {"heading": self.last_heading})]
        return []

    async def ros_send(self, payload: dict[str, Any]) -> None:
        if not self.ros_ws:
            return
        await self.ros_ws.send(json.dumps(payload))


def ros1_to_ros2_type(msg_type: str) -> str:
    if "/" not in msg_type or "/msg/" in msg_type:
        return msg_type
    package, name = msg_type.split("/", 1)
    return f"{package}/msg/{name}"


def topic_for_vehicle(topic: str) -> str:
    clean = topic.strip("/")
    if clean.startswith("mavros/"):
        return f"/vehicles/{VEHICLE_ID}/{clean}"
    return f"/vehicles/{VEHICLE_ID}/{clean or 'unknown'}"


def global_position_target(target: dict[str, float], yaw_deg: float | None) -> dict[str, Any]:
    ignore_velocity = 8 | 16 | 32
    ignore_accel = 64 | 128 | 256
    ignore_yaw_rate = 2048
    type_mask = ignore_velocity | ignore_accel | ignore_yaw_rate
    yaw = 0.0 if yaw_deg is None else yaw_deg * 3.141592653589793 / 180.0
    return {
        "header": ros_header("map"),
        "coordinate_frame": GLOBAL_SETPOINT_FRAME,
        "type_mask": type_mask,
        "latitude": target["latitude"],
        "longitude": target["longitude"],
        "altitude": target["altitude"],
        "velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
        "acceleration_or_force": {"x": 0.0, "y": 0.0, "z": 0.0},
        "yaw": yaw,
        "yaw_rate": 0.0,
    }


def ros_header(frame_id: str) -> dict[str, Any]:
    stamp = time.time()
    sec = int(stamp)
    return {"stamp": {"secs": sec, "nsecs": int((stamp - sec) * 1_000_000_000)}, "frame_id": frame_id}


if __name__ == "__main__":
    asyncio.run(Bridge().run_forever())
