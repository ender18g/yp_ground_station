from __future__ import annotations

import asyncio
import json
import os
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import websockets


SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://yp-server:8000/ws/vehicle")
VEHICLE_ID = os.getenv("VEHICLE_ID", "sim-umaa")
VEHICLE_TYPE = os.getenv("VEHICLE_TYPE", "usv")
SEND_HZ = float(os.getenv("SEND_HZ", "5"))
UMAA_BACKEND = os.getenv("UMAA_BACKEND", "loopback").lower()

LOOPBACK_LAT = float(os.getenv("LOOPBACK_LAT", "38.989639"))
LOOPBACK_LON = float(os.getenv("LOOPBACK_LON", "-76.478643"))
LOOPBACK_ALT = float(os.getenv("LOOPBACK_ALT", "0.0"))
LOOPBACK_HEADING = float(os.getenv("LOOPBACK_HEADING", "0.0"))
LOOPBACK_BATTERY = float(os.getenv("LOOPBACK_BATTERY", "1.0"))
LOOPBACK_SPEED_MPS = float(os.getenv("LOOPBACK_SPEED_MPS", "1.5"))
LOOPBACK_TURN_RATE_DPS = float(os.getenv("LOOPBACK_TURN_RATE_DPS", "15.0"))
LOOPBACK_ARRIVAL_RADIUS_M = float(os.getenv("LOOPBACK_ARRIVAL_RADIUS_M", "2.0"))
LOOPBACK_BATTERY_DRAIN_PER_M = float(os.getenv("LOOPBACK_BATTERY_DRAIN_PER_M", "0.00015"))
LOOPBACK_BATTERY_DRAIN_PER_S = float(os.getenv("LOOPBACK_BATTERY_DRAIN_PER_S", "0.00001"))
LOOPBACK_IDLE_BATTERY_DRAIN_PER_S = float(os.getenv("LOOPBACK_IDLE_BATTERY_DRAIN_PER_S", "0.000002"))
EARTH_RADIUS_M = 6_378_137.0

RTI_DOMAIN_ID = int(os.getenv("RTI_DOMAIN_ID", "1"))
RTI_QOS_FILE = os.getenv("RTI_QOS_FILE", "")
RTI_SOURCE_GUID = os.getenv("RTI_SOURCE_GUID", "")
RTI_COMMAND_TOPIC = os.getenv("RTI_COMMAND_TOPIC", "")
RTI_ACK_TOPIC = os.getenv("RTI_ACK_TOPIC", "")
RTI_STATUS_TOPIC = os.getenv("RTI_STATUS_TOPIC", "")
RTI_EXEC_STATUS_TOPIC = os.getenv("RTI_EXEC_STATUS_TOPIC", "")
RTI_NAVSATFIX_TOPIC = os.getenv("RTI_NAVSATFIX_TOPIC", "")
RTI_BATTERY_TOPIC = os.getenv("RTI_BATTERY_TOPIC", "")
RTI_HEARTBEAT_TOPIC = os.getenv("RTI_HEARTBEAT_TOPIC", "")
RTI_PUBLISHER_NAME = os.getenv("RTI_PUBLISHER_NAME", "")
RTI_SUBSCRIBER_NAME = os.getenv("RTI_SUBSCRIBER_NAME", "")


@dataclass(slots=True)
class TelemetryEvent:
    vehicle_id: str
    vehicle_type: str
    topic: str
    msg_type: str
    msg: dict[str, Any]
    stamp: float = field(default_factory=time.time)

    def to_payload(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "vehicle_type": self.vehicle_type,
            "topic": self.topic,
            "type": self.msg_type,
            "stamp": self.stamp,
            "msg": self.msg,
        }


class UmaaAdapter(ABC):
    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def drain_events(self) -> list[TelemetryEvent]:
        raise NotImplementedError

    @abstractmethod
    async def send_command(self, command: dict[str, Any], source: str | None = None) -> None:
        raise NotImplementedError


class LoopbackUmaaAdapter(UmaaAdapter):
    def __init__(self) -> None:
        self._running = False
        self._events: asyncio.Queue[TelemetryEvent] = asyncio.Queue()
        self._lat = LOOPBACK_LAT
        self._lon = LOOPBACK_LON
        self._alt = LOOPBACK_ALT
        self._heading = LOOPBACK_HEADING % 360.0
        self._battery = max(0.0, min(1.0, LOOPBACK_BATTERY))
        self._last_tick = time.time()
        self._target: dict[str, float] | None = None
        self._mission_queue: list[dict[str, float]] = []
        self._mode = "idle"

    async def start(self) -> None:
        self._running = True
        self._last_tick = time.time()
        await self._queue_baseline()

    async def stop(self) -> None:
        self._running = False

    @staticmethod
    def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        dlat = lat2_rad - lat1_rad
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2.0) ** 2
        return 2.0 * EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))

    @staticmethod
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

    def _advance_state(self) -> None:
        now = time.time()
        dt = max(0.0, min(1.0, now - self._last_tick))
        self._last_tick = now

        if dt <= 0.0:
            return

        moved_m = 0.0
        if self._target is not None:
            target_lat = self._target["latitude"]
            target_lon = self._target["longitude"]
            target_alt = self._target["altitude"]
            distance_m = self._distance_m(self._lat, self._lon, target_lat, target_lon)
            if distance_m <= LOOPBACK_ARRIVAL_RADIUS_M:
                self._lat = target_lat
                self._lon = target_lon
                self._alt = target_alt
                if self._mission_queue:
                    self._target = self._mission_queue.pop(0)
                    self._mode = "mission"
                else:
                    self._mode = "holding"
                    self._target = None
            else:
                step_m = min(distance_m, LOOPBACK_SPEED_MPS * dt)
                bearing_deg = math.degrees(math.atan2(
                    math.sin(math.radians(target_lon - self._lon)) * math.cos(math.radians(target_lat)),
                    math.cos(math.radians(self._lat)) * math.sin(math.radians(target_lat))
                    - math.sin(math.radians(self._lat)) * math.cos(math.radians(target_lat)) * math.cos(math.radians(target_lon - self._lon)),
                ))
                if math.isnan(bearing_deg):
                    bearing_deg = self._heading
                self._heading = self._turn_toward(self._heading, bearing_deg, LOOPBACK_TURN_RATE_DPS * dt)
                self._lat, self._lon = self._destination_point(self._lat, self._lon, self._heading, step_m)
                self._alt += (target_alt - self._alt) * min(1.0, dt * 0.8)
                self._mode = "moving"
                moved_m = step_m
        else:
            self._mode = "holding" if self._mode == "moving" else self._mode

        drain = LOOPBACK_IDLE_BATTERY_DRAIN_PER_S * dt + LOOPBACK_BATTERY_DRAIN_PER_M * moved_m + LOOPBACK_BATTERY_DRAIN_PER_S * dt * max(0.0, LOOPBACK_SPEED_MPS)
        self._battery = max(0.0, self._battery - drain)

    @staticmethod
    def _turn_toward(current_deg: float, target_deg: float, max_delta_deg: float) -> float:
        delta = ((target_deg - current_deg + 540.0) % 360.0) - 180.0
        if abs(delta) <= max_delta_deg:
            return target_deg % 360.0
        return (current_deg + math.copysign(max_delta_deg, delta)) % 360.0

    async def drain_events(self) -> list[TelemetryEvent]:
        self._advance_state()
        events: list[TelemetryEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except asyncio.QueueEmpty:
                break

        if self._running and not events:
            await self._queue_heartbeat()
            await self._queue_navsatfix()
            await self._queue_battery()
            await self._queue_bridge_status()
            while True:
                try:
                    events.append(self._events.get_nowait())
                except asyncio.QueueEmpty:
                    break

        return events

    async def send_command(self, command: dict[str, Any], source: str | None = None) -> None:
        cmd_type = str(command.get("type") or "")
        if cmd_type in {"waypoint", "rtb_follow"}:
            target = command.get("target") or {}
            self._mission_queue = []
            self._target = {
                "latitude": float(target.get("latitude", LOOPBACK_LAT)),
                "longitude": float(target.get("longitude", LOOPBACK_LON)),
                "altitude": float(target.get("altitude", LOOPBACK_ALT)),
            }
            self._mode = "guiding"
        elif cmd_type == "rtb":
            self._mission_queue = []
            self._target = {
                "latitude": LOOPBACK_LAT,
                "longitude": LOOPBACK_LON,
                "altitude": LOOPBACK_ALT,
            }
            self._mode = "returning"
        elif cmd_type == "cancel_sar":
            self._mission_queue = []
            self._target = None
            self._mode = "holding"
        elif cmd_type == "mission_plan":
            waypoints = command.get("waypoints") or []
            parsed: list[dict[str, float]] = []
            for waypoint in waypoints:
                if not isinstance(waypoint, dict):
                    continue
                lat = waypoint.get("latitude")
                lon = waypoint.get("longitude")
                if lat is None or lon is None:
                    continue
                parsed.append(
                    {
                        "latitude": float(lat),
                        "longitude": float(lon),
                        "altitude": float(waypoint.get("altitude", LOOPBACK_ALT)),
                    }
                )
            if parsed:
                self._target = parsed[0]
                self._mission_queue = parsed[1:]
                self._mode = "mission"
        elif cmd_type in {"search_grid", "mob", "trajectory", "ship_relative_trajectory"}:
            self._mission_queue = []
            self._target = None
            self._mode = cmd_type
        elif cmd_type == "set_mode":
            mode = command.get("mode")
            if mode:
                # For UMAA bridge, map mode strings to internal mode states if needed
                # For now, just log the request
                print(f"[LOOPBACK] set_mode requested: {mode}")
        print(f"[LOOPBACK] command source={source} payload={command}")

    async def _queue_baseline(self) -> None:
        await self._events.put(
            TelemetryEvent(
                vehicle_id=VEHICLE_ID,
                vehicle_type=VEHICLE_TYPE,
                topic=f"/vehicles/{VEHICLE_ID}/status",
                msg_type="yp_ground_station/msg/BridgeStatus",
                msg={"backend": "loopback", "status": "connected", "mode": self._mode},
            )
        )

    async def _queue_heartbeat(self) -> None:
        await self._events.put(
            TelemetryEvent(
                vehicle_id=VEHICLE_ID,
                vehicle_type=VEHICLE_TYPE,
                topic=f"/vehicles/{VEHICLE_ID}/heartbeat",
                msg_type="yp_ground_station/msg/Heartbeat",
                msg={"mode": self._mode, "armed": self._target is not None},
            )
        )

    async def _queue_navsatfix(self) -> None:
        stamp = time.time()
        sec = int(stamp)
        nanosec = int((stamp - sec) * 1_000_000_000)
        await self._events.put(
            TelemetryEvent(
                vehicle_id=VEHICLE_ID,
                vehicle_type=VEHICLE_TYPE,
                topic=f"/vehicles/{VEHICLE_ID}/navsatfix",
                msg_type="sensor_msgs/msg/NavSatFix",
                msg={
                    "header": {"stamp": {"sec": sec, "nanosec": nanosec}, "frame_id": "map"},
                    "status": {"status": 0, "service": 1},
                    "latitude": self._lat,
                    "longitude": self._lon,
                    "altitude": self._alt,
                    "position_covariance": [0.0] * 9,
                    "position_covariance_type": 0,
                    "heading": self._heading,
                },
            )
        )

    async def _queue_battery(self) -> None:
        await self._events.put(
            TelemetryEvent(
                vehicle_id=VEHICLE_ID,
                vehicle_type=VEHICLE_TYPE,
                topic=f"/vehicles/{VEHICLE_ID}/battery",
                msg_type="sensor_msgs/msg/BatteryState",
                msg={"percentage": self._battery, "voltage": 24.0, "current": 0.0},
            )
        )

    async def _queue_bridge_status(self) -> None:
        await self._events.put(
            TelemetryEvent(
                vehicle_id=VEHICLE_ID,
                vehicle_type=VEHICLE_TYPE,
                topic=f"/vehicles/{VEHICLE_ID}/status",
                msg_type="yp_ground_station/msg/BridgeStatus",
                msg={
                    "backend": "loopback",
                    "status": "connected" if self._battery > 0 else "low_battery",
                    "mode": self._mode,
                    "target_active": self._target is not None,
                    "battery": round(self._battery, 4),
                },
            )
        )


class RtiConnextUmaaAdapter(UmaaAdapter):
    def __init__(self) -> None:
        try:
            import rti.connextdds  # noqa: F401
        except ImportError as exc:
            raise SystemExit(
                "rti.connextdds is required for UMAA DDS bridging. Install the RTI Connext Python SDK and source its environment before starting this bridge."
            ) from exc

        missing = [
            name
            for name, value in {
                "RTI_COMMAND_TOPIC": RTI_COMMAND_TOPIC,
                "RTI_ACK_TOPIC": RTI_ACK_TOPIC,
                "RTI_STATUS_TOPIC": RTI_STATUS_TOPIC,
                "RTI_NAVSATFIX_TOPIC": RTI_NAVSATFIX_TOPIC,
                "RTI_BATTERY_TOPIC": RTI_BATTERY_TOPIC,
                "RTI_HEARTBEAT_TOPIC": RTI_HEARTBEAT_TOPIC,
            }.items()
            if not value.strip()
        ]
        if missing:
            raise SystemExit(
                "RTI backend is configured, but these environment variables are still unset: "
                + ", ".join(missing)
                + ". Set the UMAA topic names for your vehicle profile before using UMAA_BACKEND=rti."
            )

        self._events: asyncio.Queue[TelemetryEvent] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._running = False
        self._topic_map = {
            "command": RTI_COMMAND_TOPIC,
            "ack": RTI_ACK_TOPIC,
            "status": RTI_STATUS_TOPIC,
            "navsatfix": RTI_NAVSATFIX_TOPIC,
            "battery": RTI_BATTERY_TOPIC,
            "heartbeat": RTI_HEARTBEAT_TOPIC,
        }

    async def start(self) -> None:
        self._running = True
        print(
            "[DDS] RTI backend configured for domain "
            f"{RTI_DOMAIN_ID} with QoS file={RTI_QOS_FILE or '<default>'}"
        )
        self._reader_task = asyncio.create_task(self._reader_loop(), name=f"umaa-dds-reader-{VEHICLE_ID}")

    async def stop(self) -> None:
        self._running = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

    async def drain_events(self) -> list[TelemetryEvent]:
        events: list[TelemetryEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    async def send_command(self, command: dict[str, Any], source: str | None = None) -> None:
        print(f"[DDS] command source={source} topic={self._topic_map['command']} payload={command}")
        raise NotImplementedError(
            "Wire this adapter to the UMAA command provider/consumer types from rticonnextdds-usecases-umaa. "
            "This starter now validates the required topic configuration and provides the mapping surface."
        )

    async def _reader_loop(self) -> None:
        # Replace this with the specific UMAA readers for your vehicle.
        # The bridge harness expects TelemetryEvent instances to be enqueued here.
        while self._running:
            await asyncio.sleep(1.0)


def build_adapter() -> UmaaAdapter:
    if UMAA_BACKEND in {"rti", "rticonnext", "dds"}:
        return RtiConnextUmaaAdapter()
    return LoopbackUmaaAdapter()


async def send_vehicle_payload(ws: websockets.WebSocketClientProtocol, event: TelemetryEvent) -> None:
    await ws.send(json.dumps(event.to_payload()))


async def run_bridge() -> None:
    adapter = build_adapter()
    await adapter.start()

    uri = f"{SERVER_WS_URL.rstrip('/')}/{VEHICLE_ID}"
    print(f"[INFO] UMAA bridge backend={UMAA_BACKEND} vehicle={VEHICLE_ID}")
    print(f"[INFO] WebSocket URL: {uri}")

    try:
        while True:
            try:
                async with websockets.connect(uri, ping_interval=10, ping_timeout=10) as ws:
                    print(f"[INFO] Connected to YP server as {VEHICLE_ID}")
                    while True:
                        for event in await adapter.drain_events():
                            await send_vehicle_payload(ws, event)

                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0 / max(SEND_HZ, 1.0))
                        except asyncio.TimeoutError:
                            continue

                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        command = payload.get("command", payload)
                        if not isinstance(command, dict):
                            continue

                        await adapter.send_command(command, source=str(payload.get("source") or "ui"))
            except Exception as exc:
                print(f"[WARN] UMAA bridge reconnecting after error: {exc}")
                await asyncio.sleep(2.0)
    finally:
        await adapter.stop()


if __name__ == "__main__":
    asyncio.run(run_bridge())