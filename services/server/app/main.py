from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json
import math
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
import httpx
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from .lifeguard.manager import LifeguardManager


INFLUX_URL = os.getenv("INFLUX_URL", "http://influxdb:8086")
INFLUX_ORG = os.getenv("INFLUX_ORG", "yp")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "telemetry")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "yp-dev-token")
TILE_DIR = Path(os.getenv("TILE_DIR", "/data/tiles"))
TILE_CACHE_DIR = Path(os.getenv("TILE_CACHE_DIR", "/data/tile-cache"))
OSM_TILE_URL = os.getenv("OSM_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
OSM_USER_AGENT = os.getenv("OSM_USER_AGENT", "YPGroundStation/0.1")
OSM_REFERER = os.getenv("OSM_REFERER", "http://localhost:8080/")
EARTH_TILE_URL = os.getenv("EARTH_TILE_URL", "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}")
EARTH_USER_AGENT = os.getenv("EARTH_USER_AGENT", OSM_USER_AGENT)
EARTH_REFERER = os.getenv("EARTH_REFERER", OSM_REFERER)
MIN_TILE_TTL_SECONDS = int(os.getenv("MIN_TILE_TTL_SECONDS", str(7 * 24 * 60 * 60)))
MAX_TILE_ZOOM = int(os.getenv("MAX_TILE_ZOOM", "19"))
VEHICLE_TTL_SECONDS = float(os.getenv("VEHICLE_TTL_SECONDS", "30"))
HISTORY_MAX_POINTS = int(os.getenv("HISTORY_MAX_POINTS", "5000"))
LIFEGUARD_CONFIG_PATH = Path(os.getenv("LIFEGUARD_CONFIG_PATH", "/data/lifeguard_config.json"))
YP_VEHICLE_ID = os.getenv("YP_VEHICLE_ID", "yp")
KNOWN_BLOCKED_TILE_SHA1 = {
    "0cfb5f443183efc5921f61005aaa7f341fcfd143",
}
FALLBACK_TILE_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256"><rect width="256" height="256" fill="#dbeafe"/></svg>"""

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
tile_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

influx_client: Optional[InfluxDBClient] = None
tile_http_client: Optional[httpx.AsyncClient] = None
write_api = None

lifeguard_manager: Optional[LifeguardManager] = None
lifeguard_event_queue: asyncio.Queue = asyncio.Queue()


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": "YP Ground Station API",
        "status": "ok",
        "web_ui": "http://localhost:8080",
        "docs": "/docs",
        "health": "/health",
        "vehicles": "/api/vehicles",
        "tile_cache": "/api/tile-cache",
    }


@app.on_event("startup")
async def startup() -> None:
    global influx_client, tile_http_client, write_api, lifeguard_manager
    TILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tile_http_client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    try:
        influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        write_api = influx_client.write_api(write_options=SYNCHRONOUS)
    except Exception as exc:
        print(f"InfluxDB unavailable at startup: {exc}")

    cfg = _load_lifeguard_config()
    loop = asyncio.get_event_loop()
    lifeguard_manager = LifeguardManager(cfg, lifeguard_event_queue, loop)
    asyncio.create_task(_lifeguard_event_pump())
    # Connect agents in a thread so blocking MAVLink calls don't stall the event loop.
    asyncio.create_task(asyncio.to_thread(lifeguard_manager.start))


@app.on_event("shutdown")
async def shutdown() -> None:
    if lifeguard_manager:
        await asyncio.to_thread(lifeguard_manager.stop)
    if tile_http_client:
        await tile_http_client.aclose()
    if influx_client:
        influx_client.close()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/lifeguard/config")
async def lifeguard_config_get() -> JSONResponse:
    return JSONResponse(_load_lifeguard_config())


@app.post("/api/lifeguard/config")
async def lifeguard_config_set(body: dict) -> JSONResponse:
    _save_lifeguard_config(body)
    if lifeguard_manager:
        lifeguard_manager._settings = body
    return JSONResponse({"ok": True})


@app.get("/api/lifeguard/agents")
async def lifeguard_agents_get() -> JSONResponse:
    agents = lifeguard_manager.get_agent_states() if lifeguard_manager else []
    return JSONResponse({"agents": agents})


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


@app.get("/api/tile-cache")
async def tile_cache_status() -> dict[str, Any]:
    providers = {
        "osm": {
            "name": "OpenStreetMap",
            "source_url": OSM_TILE_URL,
        },
        "earth": {
            "name": "Earth View",
            "source_url": EARTH_TILE_URL,
        },
    }
    provider_status = {}
    total_tiles = 0
    total_bytes = 0
    for provider, details in providers.items():
        files = list((TILE_CACHE_DIR / provider).glob("*/*/*.png"))
        bytes_used = sum(path.stat().st_size for path in files)
        total_tiles += len(files)
        total_bytes += bytes_used
        provider_status[provider] = details | {"tiles": len(files), "bytes": bytes_used}
    return {
        "cache_dir": str(TILE_CACHE_DIR),
        "providers": provider_status,
        "tiles": total_tiles,
        "bytes": total_bytes,
        "min_ttl_seconds": MIN_TILE_TTL_SECONDS,
    }


@app.get("/tiles/{z}/{x}/{y}.png", response_model=None)
async def tiles(z: int, x: int, y: int):
    tile_path = TILE_DIR / str(z) / str(x) / f"{y}.png"
    if not tile_path.is_file():
        return JSONResponse({"error": "offline tile not found"}, status_code=404)
    if hashlib.sha1(tile_path.read_bytes()).hexdigest() in KNOWN_BLOCKED_TILE_SHA1:
        return JSONResponse({"error": "offline tile is a known blocked placeholder"}, status_code=404)
    return FileResponse(tile_path, media_type="image/png")


@app.get("/tiles/osm/{z}/{x}/{y}.png", response_model=None)
async def cached_osm_tile(z: int, x: int, y: int):
    return await cached_provider_tile(
        provider="osm",
        source_name="openstreetmap",
        source_url=OSM_TILE_URL,
        user_agent=OSM_USER_AGENT,
        referer=OSM_REFERER,
        z=z,
        x=x,
        y=y,
    )


@app.get("/tiles/earth/{z}/{x}/{y}.png", response_model=None)
async def cached_earth_tile(z: int, x: int, y: int):
    return await cached_provider_tile(
        provider="earth",
        source_name="earth-view",
        source_url=EARTH_TILE_URL,
        user_agent=EARTH_USER_AGENT,
        referer=EARTH_REFERER,
        z=z,
        x=x,
        y=y,
    )


@app.get("/tiles/cache/{z}/{x}/{y}.png", response_model=None)
async def cache_only_tile(z: int, x: int, y: int):
    return cache_only_provider_tile("osm", "openstreetmap", z, x, y)


@app.get("/tiles/earth-cache/{z}/{x}/{y}.png", response_model=None)
async def earth_cache_only_tile(z: int, x: int, y: int):
    return cache_only_provider_tile("earth", "earth-view", z, x, y)


async def cached_provider_tile(
    provider: str,
    source_name: str,
    source_url: str,
    user_agent: str,
    referer: str,
    z: int,
    x: int,
    y: int,
):
    validation_error = validate_tile_coordinates(z, x, y)
    if validation_error:
        return JSONResponse({"error": validation_error}, status_code=400)

    cache_path = provider_tile_path(provider, z, x, y)
    metadata_path = provider_metadata_path(provider, z, x, y)
    lock = tile_locks[f"{provider}/{z}/{x}/{y}"]

    async with lock:
        metadata = read_tile_metadata(metadata_path)
        if is_usable_cached_tile(cache_path) and not tile_expired(metadata):
            return tile_file_response(cache_path, metadata, cache_status="hit", source_name=source_name)

        result = await fetch_and_cache_tile(source_name, source_url, user_agent, referer, z, x, y, cache_path, metadata_path, metadata)
        if result:
            return result

        if is_usable_cached_tile(cache_path):
            stale_metadata = read_tile_metadata(metadata_path)
            return tile_file_response(cache_path, stale_metadata, cache_status="stale", source_name=source_name)

    return fallback_tile_response("unavailable")


def cache_only_provider_tile(provider: str, source_name: str, z: int, x: int, y: int):
    validation_error = validate_tile_coordinates(z, x, y)
    if validation_error:
        return fallback_tile_response("invalid")

    cache_path = provider_tile_path(provider, z, x, y)
    metadata = read_tile_metadata(provider_metadata_path(provider, z, x, y))
    if is_usable_cached_tile(cache_path):
        return tile_file_response(cache_path, metadata, cache_status="hit", source_name=source_name)
    return fallback_tile_response("empty")


def validate_tile_coordinates(z: int, x: int, y: int) -> Optional[str]:
    if z < 0 or z > MAX_TILE_ZOOM:
        return f"zoom must be between 0 and {MAX_TILE_ZOOM}"
    limit = 2**z
    if x < 0 or x >= limit or y < 0 or y >= limit:
        return "tile coordinates are outside the valid slippy-map range"
    return None


def provider_tile_path(provider: str, z: int, x: int, y: int) -> Path:
    return TILE_CACHE_DIR / provider / str(z) / str(x) / f"{y}.png"


def provider_metadata_path(provider: str, z: int, x: int, y: int) -> Path:
    return TILE_CACHE_DIR / provider / str(z) / str(x) / f"{y}.json"


def read_tile_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def write_tile_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True))


def tile_expired(metadata: dict[str, Any]) -> bool:
    return time.time() >= float(metadata.get("expires_at", 0))


def is_usable_cached_tile(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest() not in KNOWN_BLOCKED_TILE_SHA1
    except Exception:
        return False


async def fetch_and_cache_tile(
    source_name: str,
    source_url: str,
    user_agent: str,
    referer: str,
    z: int,
    x: int,
    y: int,
    cache_path: Path,
    metadata_path: Path,
    metadata: dict[str, Any],
):
    if not tile_http_client:
        return None

    headers = {
        "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
        "User-Agent": user_agent,
        "Referer": referer,
    }
    if metadata.get("etag"):
        headers["If-None-Match"] = str(metadata["etag"])
    if metadata.get("last_modified"):
        headers["If-Modified-Since"] = str(metadata["last_modified"])

    url = source_url.format(z=z, x=x, y=y)
    try:
        response = await tile_http_client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        print(f"{source_name} tile fetch failed {z}/{x}/{y}: {exc}")
        return None

    if response.status_code == 304 and is_usable_cached_tile(cache_path):
        refreshed = metadata | {"fetched_at": time.time(), "expires_at": tile_expires_at(response.headers)}
        write_tile_metadata(metadata_path, refreshed)
        return tile_file_response(cache_path, refreshed, cache_status="revalidated", source_name=source_name)

    if response.status_code != 200:
        print(f"{source_name} tile fetch failed {z}/{x}/{y}: HTTP {response.status_code}")
        return None

    content_type = response.headers.get("content-type", "")
    if "image" not in content_type:
        print(f"{source_name} tile fetch failed {z}/{x}/{y}: unexpected content-type {content_type}")
        return None

    data = response.content
    if hashlib.sha1(data).hexdigest() in KNOWN_BLOCKED_TILE_SHA1:
        print(f"{source_name} tile fetch blocked {z}/{x}/{y}: provider returned access-blocked placeholder")
        return None

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(".tmp")
    temp_path.write_bytes(data)
    temp_path.replace(cache_path)

    new_metadata = {
        "source_url": url,
        "fetched_at": time.time(),
        "expires_at": tile_expires_at(response.headers),
        "etag": response.headers.get("etag"),
        "last_modified": response.headers.get("last-modified"),
        "cache_control": response.headers.get("cache-control"),
        "content_type": content_type,
    }
    write_tile_metadata(metadata_path, new_metadata)
    return tile_file_response(cache_path, new_metadata, cache_status="miss", source_name=source_name)


def tile_expires_at(headers: httpx.Headers) -> float:
    cache_control = headers.get("cache-control", "")
    for part in cache_control.split(","):
        part = part.strip().lower()
        if part.startswith("max-age="):
            try:
                return time.time() + max(0, int(part.split("=", 1)[1]))
            except ValueError:
                pass

    expires = headers.get("expires")
    if expires:
        try:
            return email.utils.parsedate_to_datetime(expires).timestamp()
        except Exception:
            pass

    return time.time() + MIN_TILE_TTL_SECONDS


def tile_file_response(path: Path, metadata: dict[str, Any], cache_status: str, source_name: str) -> FileResponse:
    max_age = max(60, int(float(metadata.get("expires_at", time.time() + 60)) - time.time()))
    return FileResponse(
        path,
        media_type=str(metadata.get("content_type") or "image/png").split(";", 1)[0],
        headers={
            "Cache-Control": f"public, max-age={max_age}",
            "X-Tile-Cache": cache_status,
            "X-Tile-Source": source_name,
        },
    )


def fallback_tile_response(cache_status: str) -> Response:
    return Response(
        content=FALLBACK_TILE_SVG,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "no-store",
            "X-Tile-Cache": cache_status,
            "X-Tile-Source": "fallback",
        },
    )


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
        # Send current Lifeguard state to the newly connected client.
        if lifeguard_manager:
            await websocket.send_json({"op": "lifeguard_agents", "agents": lifeguard_manager.get_agent_states()})
            await websocket.send_json({"op": "lifeguard_config", "config": _load_lifeguard_config()})
        while True:
            payload = await websocket.receive_json()
            op = payload.get("op")
            if op == "command":
                await route_command(payload.get("vehicle_id"), payload.get("command", {}), source="ui")
            elif op == "lifeguard_command":
                await _handle_lifeguard_command(payload)
            elif op == "lifeguard_config_get":
                await websocket.send_json({"op": "lifeguard_config", "config": _load_lifeguard_config()})
            elif op == "lifeguard_config_set":
                cfg = payload.get("config", {})
                _save_lifeguard_config(cfg)
                if lifeguard_manager:
                    lifeguard_manager._settings = cfg
                await broadcast_ui({"op": "lifeguard_config", "config": cfg})
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

    # Feed ship GPS into the Lifeguard MOB track when the YP vehicle position updates.
    if (
        lifeguard_manager is not None
        and vehicle_id == YP_VEHICLE_ID
        and "NavSatFix" in msg_type
        and isinstance(msg, dict)
        and "latitude" in msg
        and "longitude" in msg
    ):
        lifeguard_manager.update_ship_position(
            float(msg["latitude"]), float(msg["longitude"])
        )

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


# ---------------------------------------------------------------------------
# Lifeguard helpers
# ---------------------------------------------------------------------------

_DEFAULT_LIFEGUARD_CFG: dict[str, Any] = {
    "agents": [],
    "mission": {"default_waypoint_altitude": 30.0, "default_swath_width": 20.0},
    "mavlink": {"baudrate": 57600, "source_system_id": 252},
    "ship": {
        "track_history_minutes": 30,
        "mob_corridor_half_width_m": 50.0,
        "mob_takeoff_altitude_m": 100.0,
        "mob_climb_speed_ms": 8.0,
    },
}


def _load_lifeguard_config() -> dict[str, Any]:
    """Load Lifeguard config from disk; return defaults when the file is absent."""
    try:
        if LIFEGUARD_CONFIG_PATH.exists():
            raw = LIFEGUARD_CONFIG_PATH.read_text()
            return json.loads(raw)
    except Exception as exc:
        print(f"Lifeguard config read error: {exc}")
    return _DEFAULT_LIFEGUARD_CFG.copy()


def _save_lifeguard_config(cfg: dict[str, Any]) -> None:
    try:
        LIFEGUARD_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIFEGUARD_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception as exc:
        print(f"Lifeguard config write error: {exc}")


async def _handle_lifeguard_command(payload: dict[str, Any]) -> None:
    """Dispatch a lifeguard command received from a browser client."""
    if not lifeguard_manager:
        return
    cmd = payload.get("command", "")
    if cmd == "grid_search":
        await asyncio.to_thread(
            lifeguard_manager.execute_grid_search,
            payload["agent_id"],
            float(payload["lat"]),
            float(payload["lon"]),
            float(payload.get("grid_size_m", 200.0)),
            float(payload.get("swath_m", 20.0)),
            float(payload.get("altitude_m", 30.0)),
        )
    elif cmd == "mob":
        await asyncio.to_thread(lifeguard_manager.execute_mob)
    elif cmd == "fly_to":
        await asyncio.to_thread(
            lifeguard_manager.execute_fly_to,
            payload["agent_id"],
            float(payload["lat"]),
            float(payload["lon"]),
            float(payload.get("altitude_m", 30.0)),
        )
    elif cmd == "rtb":
        await asyncio.to_thread(lifeguard_manager.execute_rtb, payload["agent_id"])
    elif cmd == "connect_agent":
        await asyncio.to_thread(
            lifeguard_manager.connect_agent,
            payload["name"],
            payload["connection_string"],
            payload.get("frame_type", "UAV"),
        )
    elif cmd == "disconnect_agent":
        await asyncio.to_thread(lifeguard_manager.disconnect_agent, payload["agent_id"])


async def _lifeguard_event_pump() -> None:
    """Drain the LifeguardManager's event queue and forward events to the UI."""
    while True:
        try:
            event = await lifeguard_event_queue.get()
            op = event.get("op", "")
            if op == "lifeguard_vehicle_update":
                # Route drone positions through the shared vehicle tracking machinery.
                await ingest_vehicle_message(event["payload"])
            else:
                await broadcast_ui(event)
        except Exception as exc:
            print(f"Lifeguard event pump error: {exc}")


# ---------------------------------------------------------------------------

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
