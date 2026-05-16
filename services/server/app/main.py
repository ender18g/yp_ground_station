from __future__ import annotations

import asyncio
import hashlib
import math
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS


INFLUX_URL = os.getenv("INFLUX_URL", "http://influxdb:8086")
INFLUX_ORG = os.getenv("INFLUX_ORG", "yp")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "telemetry")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "yp-dev-token")
TILE_DIR = Path(os.getenv("TILE_DIR", "/data/tiles"))
VEHICLE_TTL_SECONDS = float(os.getenv("VEHICLE_TTL_SECONDS", "30"))
HISTORY_MAX_POINTS = int(os.getenv("HISTORY_MAX_POINTS", "5000"))
KNOWN_BLOCKED_TILE_SHA1 = {
    "0cfb5f443183efc5921f61005aaa7f341fcfd143",
}

app = FastAPI(title="YP Ground Station", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

vehicles: dict[str, dict[str, Any]] = {}
vehicle_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
ui_connections: set[WebSocket] = set()
ros_connections: dict[WebSocket, set[str]] = defaultdict(set)
state_lock = asyncio.Lock()

influx_client: Optional[InfluxDBClient] = None
write_api = None


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": "YP Ground Station API",
        "status": "ok",
        "web_ui": "http://localhost:8080",
        "docs": "/docs",
        "health": "/health",
        "vehicles": "/api/vehicles",
    }


@app.on_event("startup")
def startup() -> None:
    global influx_client, write_api
    try:
        influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        write_api = influx_client.write_api(write_options=SYNCHRONOUS)
    except Exception as exc:
        print(f"InfluxDB unavailable at startup: {exc}")


@app.on_event("shutdown")
def shutdown() -> None:
    if influx_client:
        influx_client.close()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/vehicles")
async def get_vehicles() -> dict[str, Any]:
    async with state_lock:
        return {"vehicles": [public_vehicle(vehicle) for vehicle in vehicles.values()]}


@app.get("/api/vehicles/{vehicle_id}")
async def get_vehicle(vehicle_id: str) -> JSONResponse:
    async with state_lock:
        vehicle = vehicles.get(vehicle_id)
    if not vehicle:
        return JSONResponse({"error": "vehicle not found"}, status_code=404)
    return JSONResponse(public_vehicle(vehicle))


@app.get("/tiles/{z}/{x}/{y}.png", response_model=None)
async def tiles(z: int, x: int, y: int):
    tile_path = TILE_DIR / str(z) / str(x) / f"{y}.png"
    if not tile_path.is_file():
        return JSONResponse({"error": "offline tile not found"}, status_code=404)
    if hashlib.sha1(tile_path.read_bytes()).hexdigest() in KNOWN_BLOCKED_TILE_SHA1:
        return JSONResponse({"error": "offline tile is a known blocked placeholder"}, status_code=404)
    return FileResponse(tile_path, media_type="image/png")


@app.websocket("/ws/vehicle/{vehicle_id}")
async def vehicle_ws(websocket: WebSocket, vehicle_id: str) -> None:
    await websocket.accept()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    vehicle_queues[vehicle_id] = queue

    async def receive_loop() -> None:
        while True:
            payload = await websocket.receive_json()
            payload.setdefault("vehicle_id", vehicle_id)
            await ingest_vehicle_message(payload)

    async def send_loop() -> None:
        while True:
            command = await queue.get()
            await websocket.send_json(command)

    try:
        await asyncio.gather(receive_loop(), send_loop())
    except WebSocketDisconnect:
        pass
    finally:
        vehicle_queues.pop(vehicle_id, None)
        async with state_lock:
            if vehicle_id in vehicles:
                vehicles[vehicle_id]["connected"] = False
                vehicles[vehicle_id]["last_seen_age"] = time.time() - vehicles[vehicle_id].get("last_seen", time.time())
        await broadcast_ui({"op": "vehicle_disconnected", "vehicle_id": vehicle_id})


@app.websocket("/ws/ui")
async def ui_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    ui_connections.add(websocket)
    try:
        async with state_lock:
            await websocket.send_json({"op": "snapshot", "vehicles": [public_vehicle(vehicle) for vehicle in vehicles.values()]})
        while True:
            payload = await websocket.receive_json()
            if payload.get("op") == "command":
                await route_command(payload.get("vehicle_id"), payload.get("command", {}), source="ui")
    except WebSocketDisconnect:
        pass
    finally:
        ui_connections.discard(websocket)


@app.websocket("/ws/rosbridge")
async def rosbridge_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    ros_connections[websocket] = set()
    try:
        await websocket.send_json({"op": "status", "level": "info", "msg": "yp rosbridge-lite connected"})
        while True:
            payload = await websocket.receive_json()
            op = payload.get("op")
            topic = payload.get("topic")
            if op == "subscribe" and topic:
                ros_connections[websocket].add(topic)
                await websocket.send_json({"op": "status", "level": "info", "msg": f"subscribed {topic}"})
            elif op == "unsubscribe" and topic:
                ros_connections[websocket].discard(topic)
            elif op == "publish" and topic:
                msg = ros_publish_to_vehicle_message(payload)
                await ingest_vehicle_message(msg)
            elif op == "command":
                await route_command(payload.get("vehicle_id"), payload.get("command", {}), source="rosbridge")
    except WebSocketDisconnect:
        pass
    finally:
        ros_connections.pop(websocket, None)


async def ingest_vehicle_message(payload: dict[str, Any]) -> None:
    now = float(payload.get("stamp") or time.time())
    vehicle_id = str(payload.get("vehicle_id") or topic_vehicle_id(payload.get("topic", "")) or "unknown")
    vehicle_type = normalize_vehicle_type(payload.get("vehicle_type") or infer_vehicle_type(vehicle_id))
    topic = str(payload.get("topic") or f"/vehicles/{vehicle_id}/unknown")
    msg_type = str(payload.get("type") or payload.get("msg_type") or "unknown")
    msg = payload.get("msg", {})

    update: dict[str, Any] = {
        "vehicle_id": vehicle_id,
        "vehicle_type": vehicle_type,
        "topic": topic,
        "type": msg_type,
        "stamp": now,
        "msg": msg,
    }

    async with state_lock:
        vehicle = vehicles.setdefault(
            vehicle_id,
            {
                "vehicle_id": vehicle_id,
                "vehicle_type": vehicle_type,
                "connected": True,
                "last_seen": now,
                "messages": {},
                "history": deque(maxlen=HISTORY_MAX_POINTS),
            },
        )
        vehicle["vehicle_type"] = vehicle_type
        vehicle["connected"] = True
        vehicle["last_seen"] = now
        vehicle["last_seen_age"] = 0
        vehicle["messages"][topic] = {"type": msg_type, "stamp": now, "msg": msg}

        nav = extract_navsatfix(topic, msg_type, msg)
        if nav:
            vehicle["position"] = nav
            vehicle["history"].append({"stamp": now, **nav})

        pose = extract_pose(topic, msg_type, msg)
        if pose:
            vehicle["pose"] = pose
            if "heading" not in vehicle and pose.get("yaw_deg") is not None:
                vehicle["heading"] = pose["yaw_deg"]

        heading = extract_heading(msg)
        if heading is not None:
            vehicle["heading"] = heading

        battery = extract_battery(topic, msg_type, msg)
        if battery:
            vehicle["battery"] = battery

        vehicle_snapshot = public_vehicle(vehicle)

    write_influx(update)
    await broadcast_ui({"op": "vehicle_update", "vehicle": vehicle_snapshot, "message": update})
    await broadcast_ros(topic, msg, msg_type)


async def route_command(vehicle_id: Optional[str], command: dict[str, Any], source: str) -> None:
    if not vehicle_id:
        return
    payload = {
        "op": "command",
        "vehicle_id": vehicle_id,
        "source": source,
        "stamp": time.time(),
        "command": command,
    }
    queue = vehicle_queues.get(vehicle_id)
    if queue:
        await queue.put(payload)
        payload["delivered"] = True
    else:
        payload["delivered"] = False
    write_influx(
        {
            "vehicle_id": vehicle_id,
            "vehicle_type": infer_vehicle_type(vehicle_id),
            "topic": f"/vehicles/{vehicle_id}/commands",
            "type": "yp_ground_station/Command",
            "stamp": payload["stamp"],
            "msg": command,
        }
    )
    await broadcast_ui({"op": "command_ack", **payload})
    await broadcast_ros(f"/vehicles/{vehicle_id}/commands", command, "yp_ground_station/Command")


async def broadcast_ui(payload: dict[str, Any]) -> None:
    stale: list[WebSocket] = []
    for websocket in list(ui_connections):
        try:
            await websocket.send_json(payload)
        except Exception:
            stale.append(websocket)
    for websocket in stale:
        ui_connections.discard(websocket)


async def broadcast_ros(topic: str, msg: dict[str, Any], msg_type: str) -> None:
    stale: list[WebSocket] = []
    for websocket, topics in list(ros_connections.items()):
        if topic not in topics and "*" not in topics:
            continue
        try:
            await websocket.send_json({"op": "publish", "topic": topic, "type": msg_type, "msg": msg})
        except Exception:
            stale.append(websocket)
    for websocket in stale:
        ros_connections.pop(websocket, None)


def write_influx(payload: dict[str, Any]) -> None:
    if not write_api:
        return
    try:
        point = (
            Point("yp_messages")
            .tag("vehicle_id", str(payload.get("vehicle_id", "unknown")))
            .tag("vehicle_type", str(payload.get("vehicle_type", "unknown")))
            .tag("topic", str(payload.get("topic", "unknown")))
            .tag("msg_type", str(payload.get("type", "unknown")))
            .time(int(float(payload.get("stamp") or time.time()) * 1_000_000_000))
        )
        add_fields(point, payload.get("msg", {}))
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
    except Exception as exc:
        print(f"Influx write failed: {exc}")


def add_fields(point: Point, value: Any, prefix: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            safe_key = f"{prefix}_{key}" if prefix else str(key)
            add_fields(point, child, safe_key)
    elif isinstance(value, list):
        for idx, child in enumerate(value[:12]):
            add_fields(point, child, f"{prefix}_{idx}")
    elif isinstance(value, bool):
        point.field(prefix or "value", value)
    elif isinstance(value, (int, float)) and math.isfinite(float(value)):
        point.field(prefix or "value", float(value))
    elif value is not None:
        text = str(value)
        if len(text) < 256:
            point.field(prefix or "value", text)


def ros_publish_to_vehicle_message(payload: dict[str, Any]) -> dict[str, Any]:
    topic = str(payload.get("topic", ""))
    vehicle_id = topic_vehicle_id(topic) or str(payload.get("vehicle_id") or "ros-vehicle")
    return {
        "vehicle_id": vehicle_id,
        "vehicle_type": normalize_vehicle_type(payload.get("vehicle_type") or infer_vehicle_type(vehicle_id)),
        "topic": topic,
        "type": payload.get("type", "unknown"),
        "stamp": time.time(),
        "msg": payload.get("msg", {}),
    }


def public_vehicle(vehicle: dict[str, Any]) -> dict[str, Any]:
    snapshot = dict(vehicle)
    if isinstance(snapshot.get("history"), deque):
        snapshot["history"] = list(snapshot["history"])
    return snapshot


def topic_vehicle_id(topic: str) -> Optional[str]:
    parts = [part for part in topic.split("/") if part]
    if len(parts) >= 2 and parts[0] == "vehicles":
        return parts[1]
    return None


def infer_vehicle_type(vehicle_id: str) -> str:
    lower = vehicle_id.lower()
    for candidate in ("uav", "usv", "uuv", "yp"):
        if candidate in lower:
            return candidate
    return "uav"


def normalize_vehicle_type(value: Any) -> str:
    text = str(value or "uav").lower()
    return text if text in {"uav", "usv", "uuv", "yp"} else "uav"


def extract_navsatfix(topic: str, msg_type: str, msg: Any) -> Optional[dict[str, float]]:
    if not isinstance(msg, dict):
        return None
    if "NavSatFix" not in msg_type and not topic.endswith("navsatfix"):
        return None
    if "latitude" not in msg or "longitude" not in msg:
        return None
    return {
        "latitude": float(msg["latitude"]),
        "longitude": float(msg["longitude"]),
        "altitude": float(msg.get("altitude", 0.0)),
    }


def extract_pose(topic: str, msg_type: str, msg: Any) -> Optional[dict[str, Any]]:
    if not isinstance(msg, dict):
        return None
    if "Pose" not in msg_type and not topic.endswith("pose"):
        return None
    orientation = msg.get("orientation", {})
    yaw_deg = quaternion_to_yaw_deg(orientation) if isinstance(orientation, dict) else None
    return {"position": msg.get("position", {}), "orientation": orientation, "yaw_deg": yaw_deg}


def extract_battery(topic: str, msg_type: str, msg: Any) -> Optional[dict[str, Any]]:
    if not isinstance(msg, dict):
        return None
    if "BatteryState" not in msg_type and not topic.endswith("battery"):
        return None
    return {
        "percentage": msg.get("percentage"),
        "voltage": msg.get("voltage"),
        "current": msg.get("current"),
    }


def extract_heading(msg: Any) -> Optional[float]:
    if not isinstance(msg, dict):
        return None
    if "heading" in msg:
        return float(msg["heading"]) % 360
    return None


def quaternion_to_yaw_deg(q: dict[str, Any]) -> Optional[float]:
    try:
        x = float(q.get("x", 0.0))
        y = float(q.get("y", 0.0))
        z = float(q.get("z", 0.0))
        w = float(q.get("w", 1.0))
        siny_cosp = 2.0 * (w * z + x * y)
        cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
        return math.degrees(math.atan2(siny_cosp, cosy_cosp)) % 360
    except Exception:
        return None
