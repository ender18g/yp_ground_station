"""FastAPI ground station server: vehicle telemetry ingest, MAVLink SITL bridges, SAR missions, tile caching, and WebSocket fan-out."""
from __future__ import annotations

import asyncio
import serial
import socket
import json
import math
import os
import queue as _stdlib_queue
import re
import threading
import time
import zlib
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

try:
    from pymavlink import mavutil as _mavutil
except ImportError:  # pragma: no cover
    _mavutil = None  # type: ignore[assignment]

try:
    import sar_missions as _sar_missions
except ImportError:  # pragma: no cover
    _sar_missions = None  # type: ignore[assignment]

from fastapi import Body, FastAPI, Header, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from app.auth import init_database, get_current_user, require_permission
from app.auth_routes import router as auth_router
from app.settings import get_deconfliction_settings, update_deconfliction_settings
from app.settings import APPLICATION_SETTING_DEFAULTS, get_application_settings, update_application_settings
from app.tiles import router as tile_router, TILE_MAX_CACHE_AGE_SECONDS

# Import deconfliction module
from app.deconfliction import DeconflictionEngine, MISSION_PRIORITY, DEFAULT_DECONFLICT_RADIUS_M


INFLUX_URL = os.getenv("INFLUX_URL", "http://influxdb:8086")
INFLUX_ORG = os.getenv("INFLUX_ORG", "yp")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "telemetry")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "yp-dev-token")
VEHICLE_TTL_SECONDS = float(os.getenv("VEHICLE_TTL_SECONDS", "30"))
HISTORY_MAX_POINTS = int(os.getenv("HISTORY_MAX_POINTS", "5000"))
MESSAGE_RETENTION_SECONDS = float(os.getenv("MESSAGE_RETENTION_SECONDS", str(10 * 60)))
MESSAGE_CLEANUP_INTERVAL_SECONDS = float(os.getenv("MESSAGE_CLEANUP_INTERVAL_SECONDS", str(10 * 60)))
INFLUX_MAX_WRITE_HZ = float(os.getenv("INFLUX_MAX_WRITE_HZ", "5"))
VIDEO_STREAMS_JSON = os.getenv("VIDEO_STREAMS_JSON", "{}")
CAMERA_DISCOVERY_PORT = int(os.getenv("CAMERA_DISCOVERY_PORT", "8889"))
CAMERA_PROBE_INTERVAL_SECONDS = float(os.getenv("CAMERA_PROBE_INTERVAL_SECONDS", "60.0"))
CAMERA_PROBE_TIMEOUT_SECONDS = float(os.getenv("CAMERA_PROBE_TIMEOUT_SECONDS", "2.0"))

# SAR defaults — override in docker-compose environment
SAR_CORRIDOR_HALF_WIDTH_M = float(os.getenv("SAR_CORRIDOR_HALF_WIDTH_M", "50.0"))
SAR_SWATH_M = float(os.getenv("SAR_SWATH_M", "20.0"))
SAR_ALTITUDE_M = float(os.getenv("SAR_ALTITUDE_M", "30.0"))
SAR_MOB_TRACK_SECONDS = float(os.getenv("SAR_MOB_TRACK_SECONDS", "120.0"))
SAR_TAKEOFF_ALT_M = float(os.getenv("SAR_TAKEOFF_ALT_M", "30.0"))
SAR_CLIMB_SPEED_MS = float(os.getenv("SAR_CLIMB_SPEED_MS", "8.0"))
LAND_ON_BOAT_HOVER_CLEARANCE_M = float(os.getenv("LAND_ON_BOAT_HOVER_CLEARANCE_M", "0.5"))
LAND_ON_BOAT_DESCENT_RATE_MS = float(os.getenv("LAND_ON_BOAT_DESCENT_RATE_MS", "0.5"))
LAND_ON_BOAT_PAD_OFFSET_M = float(os.getenv("LAND_ON_BOAT_PAD_OFFSET_M", "5.0"))
LAND_ON_BOAT_ALIGNMENT_RADIUS_M = float(os.getenv("LAND_ON_BOAT_ALIGNMENT_RADIUS_M", "1.0"))
RTB_STERN_DISTANCE_M = float(os.getenv("RTB_STERN_DISTANCE_M", "20.0"))
RTB_UPDATE_HZ = float(os.getenv("RTB_UPDATE_HZ", "2.0"))
RTB_ALTITUDE_M = float(os.getenv("RTB_ALTITUDE_M", "30.0"))
MISSION_ARRIVAL_RADIUS_M = float(os.getenv("MISSION_ARRIVAL_RADIUS_M", "12.0"))
EARTH_RADIUS_M = 6_378_137.0

# RTCM streamer variables
_rtcm_seq_id = 0
_rtcm_last_broadcast_at = 0.0
rtcm_watchdog_task: Optional[asyncio.Task[None]] = None
# Live status surfaced to the UI: state is "disabled" | "connecting" | "connected" | "stale" | "error"
rtcm_status: dict[str, Any] = {
    "state": "disabled",
    "source_type": "disabled",
    "target": None,
    "last_frame_at": None,
    "frame_count": 0,
    "bytes_total": 0,
    "error": None,
}


app = FastAPI(title="YP Ground Station", version="0.1.0")
app.include_router(tile_router)
app.include_router(auth_router)


@app.middleware("http")
async def authenticate_cookie_requests(request, call_next):
    """Expose the HttpOnly auth cookie to existing bearer-auth route handlers."""
    if not any(key.lower() == b"authorization" for key, _ in request.scope["headers"]):
        token = request.cookies.get("auth_token")
        if token:
            request.scope["headers"] = list(request.scope["headers"]) + [(b"authorization", f"Bearer {token}".encode())]
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

vehicles: dict[str, dict[str, Any]] = {}
video_streams: dict[str, dict[str, Any]] = {}
vehicle_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
ui_connections: set[WebSocket] = set()
ros_connections: dict[WebSocket, set[str]] = defaultdict(set)
state_lock = asyncio.Lock()

# Command-derived overlays are shared by all UI clients and included in their
# initial WebSocket snapshot so late joiners see the active operational plan.
shared_waypoints: dict[str, dict[str, Any]] = {}
shared_sar_patterns: dict[str, dict[str, Any]] = {}
shared_mission_plans: dict[str, list[list[float]]] = {}
shared_mission_completion_targets: dict[str, dict[str, float]] = {}

# SITL MAVLink bridge state
sitl_bridges: dict[str, asyncio.Task[None]] = {}  # vehicle_id -> running asyncio task
sitl_bridge_info: dict[str, dict[str, Any]] = {}  # vehicle_id -> status/metadata
_rtb_follow_tasks: dict[str, asyncio.Task[None]] = {} # vehicle_id -> placeholder for running return to boat (RTB) and follow boat task
_land_on_boat_tasks: dict[str, asyncio.Task[None]] = {} # vehicle_id -> placeholder for running land on boat task
_sitl_follow_guided_requests: dict[str, float] = {} # vehicle_id -> timestamp of last follow-guided request (to avoid spamming the vehicle with repeated requests)

# MAVLink MAV_TYPE -> (vehicle_type, human-readable frame name)
_MAV_TYPE_MAP: dict[int, tuple[str, str]] = {
    0: ("uav", "Generic"),
    1: ("uav", "Fixed Wing"),
    2: ("uav", "Quadrotor"),
    3: ("uav", "Coaxial Helicopter"),
    4: ("uav", "Helicopter"),
    5: ("uav", "Antenna Tracker"),
    7: ("uav", "Airship"),
    8: ("uav", "Free Balloon"),
    9: ("uav", "Rocket"),
    10: ("ugv", "Ground Rover"),
    11: ("usv", "Surface Boat"),
    12: ("uuv", "Submarine"),
    13: ("uav", "Hexarotor"),
    14: ("uav", "Octorotor"),
    15: ("uav", "Tricopter"),
    16: ("uav", "Flapping Wing"),
    19: ("uav", "VTOL Duorotor"),
    20: ("uav", "VTOL Quadrotor"),
    21: ("uav", "VTOL Tiltrotor"),
    29: ("uav", "Dodecarotor"),
    35: ("uav", "Decarotor"),
}
_MAV_AUTOPILOT_NAMES: dict[int, str] = {
    0: "Generic",
    3: "ArduPilot",
    8: "Invalid",
    12: "PX4",
}
_VALID_MAVLINK_PREFIXES = (
    "tcp:", "tcpin:", "tcpout:",
    "udpin:", "udpout:", "udpbcast:",
    "serial:",
)
_VALID_STREAM_ID_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")

influx_client: Optional[InfluxDBClient] = None
write_api = None
delete_api = None
query_api = None
cleanup_task: Optional[asyncio.Task[None]] = None
rtcm_task: Optional[asyncio.Task[None]] = None
deconfliction_task: Optional[asyncio.Task[None]] = None


# YP role assignment: any vehicle whose vehicle_id matches this value will be
# treated as vehicle_type="yp" regardless of what it reports in its messages.
# Set via POST /api/yp/role; persists for the server's lifetime.
_yp_role_vehicle_id: Optional[str] = None

settings = {
    **APPLICATION_SETTING_DEFAULTS,
    "message_retention_seconds": MESSAGE_RETENTION_SECONDS,
    "message_cleanup_interval_seconds": MESSAGE_CLEANUP_INTERVAL_SECONDS,
    "influx_max_write_hz": INFLUX_MAX_WRITE_HZ,
    "tile_max_cache_age_seconds": TILE_MAX_CACHE_AGE_SECONDS,
    "rtb_update_hz": RTB_UPDATE_HZ,
    "rtb_altitude_m": RTB_ALTITUDE_M,
}
last_influx_write_at: dict[tuple[str, str], float] = {}

# Deconfliction engine for vehicle collision avoidance
deconfliction_engine = DeconflictionEngine(enabled=False)
_deconfliction_lock = asyncio.Lock()

# Single persistent InfluxDB writer thread drains a bounded queue.
# Replaces the old approach of spawning one daemon thread per write,
# which created up to ~100 OS threads/second under normal load.
_influx_write_queue: _stdlib_queue.Queue = _stdlib_queue.Queue(maxsize=500)


def _influx_writer_loop() -> None:
    """Drain queued InfluxDB points and write them one at a time, forever."""
    while True:
        point = _influx_write_queue.get()
        if point is None:
            break
        _do_influx_write(point)


_influx_writer_thread = threading.Thread(target=_influx_writer_loop, daemon=True)
_influx_writer_thread.start()


def sanitize_stream_id(value: str) -> str:
    """Normalize a raw string into a URL-safe stream identifier."""
    text = _VALID_STREAM_ID_CHARS.sub("-", value.strip())
    text = text.strip("-").lower()
    return text or "stream"


def default_playback_url(stream_id: str) -> str:
    """Return the default HLS playback path for a stream id."""
    return f"/hls/{stream_id}/index.m3u8"


def upsert_video_stream(vehicle_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Create or update the video stream config for a vehicle and return it."""
    current = video_streams.get(vehicle_id, {})
    source_rtsp_url = str(payload.get("source_rtsp_url") or current.get("source_rtsp_url") or "").strip()
    stream_id = sanitize_stream_id(str(payload.get("stream_id") or current.get("stream_id") or vehicle_id))
    playback_url = str(payload.get("playback_url") or current.get("playback_url") or default_playback_url(stream_id)).strip()
    enabled = bool(payload.get("enabled", current.get("enabled", True)))

    entry = {
        "vehicle_id": vehicle_id,
        "stream_id": stream_id,
        "source_rtsp_url": source_rtsp_url,
        "playback_url": playback_url,
        "enabled": enabled,
    }
    video_streams[vehicle_id] = entry
    return entry


def public_video_stream(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the client-facing subset of a video stream entry (no source URL)."""
    return {
        "vehicle_id": entry.get("vehicle_id"),
        "stream_id": entry.get("stream_id"),
        "playback_url": entry.get("playback_url"),
        "enabled": bool(entry.get("enabled", True)),
    }


# ---------------------------------------------------------------------------
# MAVLink camera discovery
#
# For built-in UI-created MAVLink bridges, we optionally probe a camera host
# on TCP CAMERA_DISCOVERY_PORT (default 8889, the MediaMTX WHEP default) and,
# when reachable, publish the canonical video_stream_update record pointing
# at http://<camera-host>:8889/cam/whep.  The browser performs the actual
# WHEP/WebRTC negotiation; this probe is only a raw TCP reachability check.
# ---------------------------------------------------------------------------

# Hosts that cannot be used as a camera target because they are inbound
# listener/wildcard addresses, not a reachable peer.
_WILDCARD_HOSTS = {"0.0.0.0", "::", "*", "localhost", "127.0.0.1"}


def derive_camera_host_from_mavlink_url(mavlink_url: str) -> Optional[str]:
    """Best-effort extraction of a reachable camera host from a MAVLink URL.

    Only host-based TCP/UDP URLs (tcp:, tcpout:, udpout:, udpbcast:) yield a
    usable host. Serial URLs and inbound/wildcard listeners (tcpin:,
    udpin:, or an explicit 0.0.0.0/localhost host) return None — those
    require an explicit camera_host from the caller.
    """
    if not mavlink_url:
        return None
    lowered = mavlink_url.lower()
    if lowered.startswith("serial:"):
        return None
    if lowered.startswith("tcpin:") or lowered.startswith("udpin:"):
        return None
    if not (lowered.startswith("tcp:") or lowered.startswith("tcpout:")
            or lowered.startswith("udpout:") or lowered.startswith("udpbcast:")):
        return None

    remainder = mavlink_url.split(":", 1)[-1]
    host = remainder.rsplit(":", 1)[0] if ":" in remainder else remainder
    host = host.strip()
    if not host or host.lower() in _WILDCARD_HOSTS:
        return None
    return host


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")


def normalize_camera_host(value: Optional[str]) -> Optional[str]:
    """Validate/normalize a user-supplied or derived camera host string.

    Returns None if the value is empty. Raises ValueError if it is present
    but structurally invalid or a wildcard/listener address.
    """
    if value is None:
        return None
    host = value.strip()
    if not host:
        return None
    if host.lower() in _WILDCARD_HOSTS:
        raise ValueError(f"camera_host '{host}' is a wildcard/listener address and cannot be probed")
    if not _HOSTNAME_RE.match(host):
        raise ValueError(f"camera_host '{host}' is not a valid hostname or IP address")
    return host


def canonical_whep_url(camera_host: str) -> str:
    return f"http://{camera_host}:{CAMERA_DISCOVERY_PORT}/cam/whep"


async def probe_camera_reachable(camera_host: str, timeout: float = CAMERA_PROBE_TIMEOUT_SECONDS) -> bool:
    """Raw TCP reachability check against camera_host:CAMERA_DISCOVERY_PORT."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(camera_host, CAMERA_DISCOVERY_PORT), timeout=timeout
        )
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True


async def publish_camera_stream_if_reachable(vehicle_id: str, camera_host: str) -> bool:
    """Probe camera_host and, if reachable, upsert + broadcast the canonical
    video_stream_update record.  Returns whether the probe succeeded.

    A failed probe intentionally does not remove or overwrite any previously
    advertised stream — transient camera outages should not erase usable
    connection metadata.
    """
    reachable = await probe_camera_reachable(camera_host)
    if not reachable:
        return False
    entry = upsert_video_stream(vehicle_id, {"playback_url": canonical_whep_url(camera_host)})
    await broadcast_ui({"op": "video_stream_update", "video": public_video_stream(entry)})
    return True


def load_video_streams_from_env() -> None:
    """Populate ``video_streams`` from the VIDEO_STREAMS_JSON environment variable."""
    try:
        raw = json.loads(VIDEO_STREAMS_JSON)
    except json.JSONDecodeError as exc:
        print(f"VIDEO_STREAMS_JSON parse error: {exc}")
        return

    if not isinstance(raw, dict):
        print("VIDEO_STREAMS_JSON must be an object map of vehicle_id -> config")
        return

    for vehicle_id, value in raw.items():
        if not isinstance(vehicle_id, str) or not vehicle_id.strip():
            continue
        vid = vehicle_id.strip()
        if isinstance(value, str):
            upsert_video_stream(vid, {"source_rtsp_url": value})
            continue
        if isinstance(value, dict):
            upsert_video_stream(vid, value)


@app.get("/")
async def root() -> dict[str, Any]:
    """Return API metadata and links to key endpoints."""
    return {
        "name": "YP Ground Station API",
        "status": "ok",
        "web_ui": "http://localhost:8080",
        "docs": "/docs",
        "health": "/health",
        "vehicles": "/api/vehicles",
        "video_streams": "/api/video/streams",
        "tile_cache": "/api/tile-cache",
    }


@app.on_event("startup")
async def startup() -> None:
    """Initialize persistence, vehicle services, and background tasks."""
    global cleanup_task, delete_api, influx_client, write_api, query_api, rtcm_task, rtcm_watchdog_task, deconfliction_task
    # Initialize authentication database
    init_database()

    persisted_settings = get_application_settings()
    settings.update(persisted_settings)
    global _yp_role_vehicle_id
    _yp_role_vehicle_id = persisted_settings.get("yp_role_vehicle_id")
    
    # Load deconfliction settings from database
    db_settings = get_deconfliction_settings()
    _apply_deconfliction_settings(db_settings)
    print(f"[DECONFLICTION] Initialized: enabled={deconfliction_engine.enabled}, global_radius={deconfliction_engine.global_radius_m}m")
    
    try:
        influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        write_api = influx_client.write_api(write_options=SYNCHRONOUS)
        delete_api = influx_client.delete_api()
        query_api = influx_client.query_api()
    except Exception as exc:
        print(f"InfluxDB unavailable at startup: {exc}")
    cleanup_task = asyncio.create_task(influx_retention_loop())
    load_video_streams_from_env()
    
    # Start deconfliction check task
    deconfliction_task = asyncio.create_task(_deconfliction_check_loop())

    # Start background RTCM base station ingestion task
    rtcm_task = asyncio.create_task(rtcm_ingest_loop())
    rtcm_watchdog_task = asyncio.create_task(rtcm_watchdog_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    """Cancel background tasks and close external clients on server shutdown."""
    tasks = [
        task for task in (
            rtcm_task, rtcm_watchdog_task, cleanup_task, deconfliction_task,
            *sitl_bridges.values(), *_rtb_follow_tasks.values(),
        ) if task is not None
    ]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if influx_client:
        influx_client.close()


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe endpoint."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# SITL / MAVLink bridge endpoints
# ---------------------------------------------------------------------------

@app.get("/api/sitl")
async def list_sitl_bridges(authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """Return all active (and recently errored) SITL bridge connections. Requires manage_sitl permission."""
    authorization_error = require_permission(authorization, "manage_sitl")
    if authorization_error:
        return authorization_error
    
    return JSONResponse({"bridges": list(sitl_bridge_info.values())})


@app.post("/api/sitl")
async def connect_sitl(payload: dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """
    Open a new MAVLink bridge connection. Requires manage_sitl permission.

    Body fields:
      url         – pymavlink connection string, e.g. ``tcp:localhost:5760``,
                    ``udpin:0.0.0.0:14551``, ``udpout:host:14550``.
      vehicle_id  – optional; derived from the URL if omitted.
      camera_host – optional hostname/IP of a camera reachable at TCP 8889.
                    Defaults to the MAVLink URL's host for host-based
                    TCP/UDP connections; serial and wildcard/listener URLs
                    require this to be set explicitly. When it cannot be
                    resolved, camera discovery is simply skipped for that
                    bridge.
    """
    authorization_error = require_permission(authorization, "manage_sitl")
    if authorization_error:
        return authorization_error
    
    if _mavutil is None:
        return JSONResponse({"error": "pymavlink is not installed on this server"}, status_code=501)

    mavlink_url: str = str(payload.get("url") or "").strip()
    vehicle_id: str = re.sub(r"[^a-zA-Z0-9_-]", "-", str(payload.get("vehicle_id") or "").strip()).strip("-")
    camera_host_raw = payload.get("camera_host")

    if not mavlink_url:
        return JSONResponse({"error": "url is required"}, status_code=400)
    if not any(mavlink_url.lower().startswith(p) for p in _VALID_MAVLINK_PREFIXES):
        return JSONResponse(
            {"error": f"url must start with one of: {', '.join(_VALID_MAVLINK_PREFIXES)}"},
            status_code=400,
        )

    try:
        camera_host = normalize_camera_host(str(camera_host_raw) if camera_host_raw is not None else None)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not camera_host:
        camera_host = derive_camera_host_from_mavlink_url(mavlink_url)

    if not vehicle_id:
        # Derive a stable ID from the URL: tcp:localhost:5760 -> vehicle-localhost-5760
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", mavlink_url.split(":", 1)[-1]).strip("-")
        vehicle_id = f"vehicle-{slug}" if slug else f"vehicle-{len(sitl_bridges) + 1}"

    existing_task = sitl_bridges.get(vehicle_id)
    if existing_task and not existing_task.done():
        return JSONResponse({"error": f"A bridge for '{vehicle_id}' is already running"}, status_code=409)

    task = asyncio.create_task(_run_mavlink_bridge(vehicle_id, mavlink_url, camera_host=camera_host))
    sitl_bridges[vehicle_id] = task
    return JSONResponse({"ok": True, "vehicle_id": vehicle_id, "url": mavlink_url, "camera_host": camera_host})


@app.delete("/api/sitl/{vehicle_id}")
async def disconnect_sitl(vehicle_id: str, authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """Cancel and remove a SITL bridge connection. Requires manage_sitl permission."""
    authorization_error = require_permission(authorization, "manage_sitl")
    if authorization_error:
        return authorization_error
    
    task = sitl_bridges.get(vehicle_id)
    if not task:
        return JSONResponse({"error": "Bridge not found"}, status_code=404)
    task.cancel()
    sitl_bridges.pop(vehicle_id, None)
    sitl_bridge_info.pop(vehicle_id, None)
    vehicle_queues.pop(vehicle_id, None)
    async with state_lock:
        vehicles.pop(vehicle_id, None)
    await broadcast_ui({"op": "vehicle_removed", "vehicle_id": vehicle_id})
    await broadcast_ui({"op": "sitl_bridge_removed", "vehicle_id": vehicle_id})
    return JSONResponse({"ok": True})


@app.get("/api/serial-ports")
async def list_serial_ports_endpoint() -> dict[str, Any]:
    """Return serial ports available on the server host.

    Requires the server container to have device passthrough configured (see
    docker-compose ``devices:`` key) and pyserial installed.
    """
    try:
        import serial.tools.list_ports as _list_ports
        ports = [
            {"device": p.device, "description": p.description, "hwid": p.hwid}
            for p in _list_ports.comports()
        ]
    except ImportError:
        ports = []
    return {"ports": ports}


# ---------------------------------------------------------------------------
# SITL bridge async task
# ---------------------------------------------------------------------------

async def _camera_probe_loop(vehicle_id: str, camera_host: str) -> None:
    """Probe camera_host:CAMERA_DISCOVERY_PORT once immediately, then every
    CAMERA_PROBE_INTERVAL_SECONDS, publishing a video_stream_update on each
    successful probe. Runs until cancelled alongside the owning bridge task.
    """
    try:
        while True:
            try:
                await publish_camera_stream_if_reachable(vehicle_id, camera_host)
            except Exception as exc:
                print(f"[SITL][camera] probe error for {vehicle_id} ({camera_host}): {exc}")
            await asyncio.sleep(CAMERA_PROBE_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        pass


async def _run_mavlink_bridge(
    vehicle_id: str,
    mavlink_url: str,
    send_hz: float = 10.0,
    camera_host: Optional[str] = None,
) -> None:
    """Asyncio task: connect to a MAVLink endpoint, detect frame type, and
    stream telemetry into the ground station while forwarding commands back.

    All blocking MAVLink I/O runs in a dedicated daemon thread so recv_match
    uses blocking=True (zero poll delay).  Messages arrive in a thread-safe
    queue and are drained in batches by the asyncio side.
    """
    info: dict[str, Any] = {
        "vehicle_id": vehicle_id,
        "url": mavlink_url,
        "status": "connecting",
        "frame": None,
        "autopilot": None,
        "vehicle_type": "uav",
        "error": None,
        "camera_host": camera_host,
    }
    sitl_bridge_info[vehicle_id] = info
    camera_probe_task: Optional[asyncio.Task[None]] = None

    # Command queue registered so route_command can deliver waypoints / RTB
    cmd_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    vehicle_queues[vehicle_id] = cmd_queue

    await broadcast_ui({"op": "sitl_bridge_update", "bridge": dict(info)})

    master = None
    _stop = threading.Event()
    _inbound: _stdlib_queue.Queue[tuple[str, Any, float]] = _stdlib_queue.Queue(maxsize=500)
    _outbound: _stdlib_queue.Queue[dict[str, Any]] = _stdlib_queue.Queue(maxsize=100)

    try:
        print(f"[SITL] Connecting {vehicle_id} -> {mavlink_url}")

        def _connect_blocking():
            m = _mavutil.mavlink_connection(mavlink_url, source_system=255)
            m.wait_heartbeat(timeout=30)
            return m

        try:
            master = await asyncio.wait_for(asyncio.to_thread(_connect_blocking), timeout=35.0)
        except (asyncio.TimeoutError, Exception) as exc:
            info["status"] = "error"
            info["error"] = f"Connection failed: {exc}"
            print(f"[SITL] {vehicle_id}: {info['error']}")
            await broadcast_ui({"op": "sitl_bridge_update", "bridge": dict(info)})
            return

        # Determine vehicle type and frame from the received heartbeat
        hb = master.messages.get("HEARTBEAT")
        if hb:
            mav_type: int = int(hb.type)
            autopilot_id: int = int(hb.autopilot)
            vehicle_type, frame_name = _MAV_TYPE_MAP.get(mav_type, ("uav", f"MAV_TYPE {mav_type}"))
            autopilot_name = _MAV_AUTOPILOT_NAMES.get(autopilot_id, f"Autopilot {autopilot_id}")
            info["frame"] = frame_name
            info["autopilot"] = autopilot_name
            info["vehicle_type"] = vehicle_type
            print(f"[SITL] {vehicle_id}: frame={frame_name}, autopilot={autopilot_name}, type={vehicle_type}")
        else:
            info["vehicle_type"] = "uav"

        info["status"] = "connected"
        info["error"] = None
        await broadcast_ui({"op": "sitl_bridge_update", "bridge": dict(info)})

        if camera_host:
            camera_probe_task = asyncio.create_task(_camera_probe_loop(vehicle_id, camera_host))

        # Request position stream at the target Hz and battery at 2 Hz
        for stream_id, hz in [
            (_mavutil.mavlink.MAV_DATA_STREAM_POSITION, int(send_hz)),
            (_mavutil.mavlink.MAV_DATA_STREAM_EXTRA1, 2),
        ]:
            await asyncio.to_thread(
                lambda sid=stream_id, h=hz: master.mav.request_data_stream_send(
                    master.target_system, master.target_component, sid, h, 1
                )
            )

        # ------------------------------------------------------------------ #
        # Dedicated MAVLink I/O thread                                        #
        # Uses blocking=True so there is zero poll delay and no              #
        # asyncio.to_thread overhead per message in steady state.            #
        # ------------------------------------------------------------------ #
        def _io_thread(m: Any) -> None:
            min_pos_interval = 1.0 / send_hz
            last_pos_time = 0.0

            while not _stop.is_set():
                # Forward any outbound commands queued by the asyncio side
                while True:
                    try:
                        _handle_sitl_command(m, _outbound.get_nowait())
                    except _stdlib_queue.Empty:
                        break

                # Pause telemetry reads while a SAR mission holds the connection
                # (mission executor calls recv_match for ACKs — must not race)
                if _sar_active.is_set():
                    time.sleep(0.05)
                    continue

                # Blocking read — wakes up as soon as a message arrives
                msg = m.recv_match(
                    type=["GLOBAL_POSITION_INT", "SYS_STATUS", "BATTERY_STATUS"],
                    blocking=True,
                    timeout=0.1,
                )
                if msg is None:
                    continue

                msg_type = msg.get_type()
                now = time.time()

                # Rate-limit position messages to avoid overwhelming the UI
                if msg_type == "GLOBAL_POSITION_INT":
                    if now - last_pos_time < min_pos_interval:
                        continue
                    last_pos_time = now

                try:
                    _inbound.put_nowait((msg_type, msg, now))
                except _stdlib_queue.Full:
                    pass  # drop under extreme back-pressure

        # Event that SAR mission threads set while they hold the MAVLink
        # connection for mission upload/arm/start.  The IO thread checks this
        # before calling recv_match so the two never race on ACK messages.
        _sar_active = threading.Event()
        _sar_stop_event = threading.Event()

        io_thread = threading.Thread(target=_io_thread, args=(master,), daemon=True)
        io_thread.start()

        last_battery_pct: Optional[float] = None

        while True:
            # Route asyncio command queue -> IO thread or SAR mission thread.
            # Cap per-cycle command draining so high-rate RTB-follow updates
            # cannot starve inbound telemetry processing on the same event loop.
            queued_commands_processed = 0
            while queued_commands_processed < 8 and not cmd_queue.empty():
                try:
                    payload = cmd_queue.get_nowait()
                    queued_commands_processed += 1
                    cmd_type = payload.get("command", {}).get("type")
                    if cmd_type == "cancel_sar":
                        _sar_stop_event.set()
                        print(f"[SITL][SAR] Cancel requested for {vehicle_id}")
                        continue
                    if cmd_type in ("search_grid", "mob"):
                        # SAR missions need exclusive MAVLink access; run in a
                        # dedicated thread and signal the IO thread to pause.
                        _loop = asyncio.get_event_loop()

                        def _forward_sar_telemetry(msg: Any) -> None:
                            try:
                                _inbound.put_nowait(("GLOBAL_POSITION_INT", msg, time.time()))
                            except _stdlib_queue.Full:
                                pass

                        def _run_sar(p: dict[str, Any] = payload) -> None:
                            _sar_stop_event.clear()
                            _sar_active.set()
                            try:
                                _execute_sar_command(
                                    master,
                                    p,
                                    telemetry_callback=_forward_sar_telemetry,
                                    stop_event=_sar_stop_event,
                                )
                            except Exception as exc:
                                print(f"[SITL][SAR] Unhandled error: {exc}")
                            finally:
                                _sar_active.clear()
                        threading.Thread(target=_run_sar, daemon=True).start()
                    else:
                        _outbound.put_nowait(payload)
                except (_stdlib_queue.Full, asyncio.QueueEmpty):
                    break

            # Drain all messages that arrived since last iteration
            processed = 0
            while processed < 32:  # cap per cycle to stay fair to event loop
                try:
                    msg_type, msg, now = _inbound.get_nowait()
                except _stdlib_queue.Empty:
                    break
                processed += 1

                if msg_type == "SYS_STATUS":
                    raw = msg.battery_remaining
                    if raw >= 0:
                        last_battery_pct = raw / 100.0

                elif msg_type == "BATTERY_STATUS":
                    if msg.battery_remaining >= 0:
                        last_battery_pct = msg.battery_remaining / 100.0

                elif msg_type == "GLOBAL_POSITION_INT":
                    lat = msg.lat / 1e7
                    lon = msg.lon / 1e7
                    alt = msg.relative_alt / 1000.0
                    hdg = getattr(msg, "hdg", None)
                    heading = (hdg / 100.0) if hdg is not None and hdg != 65535 else None

                    nav_msg: dict[str, Any] = {
                        "header": {
                            "stamp": {"sec": int(now), "nanosec": int((now % 1) * 1e9)},
                            "frame_id": "map",
                        },
                        "status": {"status": 0, "service": 1},
                        "latitude": lat,
                        "longitude": lon,
                        "altitude": alt,
                        "position_covariance": [0.0] * 9,
                        "position_covariance_type": 0,
                    }
                    if heading is not None:
                        nav_msg["heading"] = heading

                    await ingest_vehicle_message({
                        "vehicle_id": vehicle_id,
                        "vehicle_type": info["vehicle_type"],
                        "topic": f"/vehicles/{vehicle_id}/navsatfix",
                        "type": "sensor_msgs/msg/NavSatFix",
                        "stamp": now,
                        "msg": nav_msg,
                    })

                    if last_battery_pct is not None:
                        await ingest_vehicle_message({
                            "vehicle_id": vehicle_id,
                            "vehicle_type": info["vehicle_type"],
                            "topic": f"/vehicles/{vehicle_id}/battery",
                            "type": "sensor_msgs/msg/BatteryState",
                            "stamp": now,
                            "msg": {"percentage": last_battery_pct},
                        })

            # Yield to event loop; shorter sleep when actively draining data
            await asyncio.sleep(0.0 if processed else 0.02)

    except asyncio.CancelledError:
        print(f"[SITL] Bridge for {vehicle_id} cancelled")
    except Exception as exc:
        info["status"] = "error"
        info["error"] = str(exc)
        print(f"[SITL] Bridge error for {vehicle_id}: {exc}")
        await broadcast_ui({"op": "sitl_bridge_update", "bridge": dict(info)})
    finally:
        if camera_probe_task is not None:
            camera_probe_task.cancel()
        _stop.set()
        if master is not None:
            master.close()       
        vehicle_queues.pop(vehicle_id, None)
        if info.get("status") == "connected":
            info["status"] = "disconnected"
            await broadcast_ui({"op": "sitl_bridge_update", "bridge": dict(info)})
        async with state_lock:
            if vehicle_id in vehicles:
                vehicles[vehicle_id]["connected"] = False
        await broadcast_ui({"op": "vehicle_disconnected", "vehicle_id": vehicle_id})


def _execute_sar_command(
    master: Any,
    cmd_payload: dict[str, Any],
    telemetry_callback: Optional[Callable[[Any], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Blocking: run a SAR mission (search_grid or mob) in the calling thread.

    Must be called from a dedicated thread that holds the MAVLink connection
    exclusively (IO thread paused via _sar_active event).
    """
    if _sar_missions is None:
        print("[SITL][SAR] sar_missions not available — ignoring SAR command")
        return
    command = cmd_payload.get("command", {})
    cmd_type = command.get("type")

    if cmd_type == "search_grid":
        lat = command.get("lat")
        lon = command.get("lon")
        grid_size_m = float(command.get("grid_size_m", 200))
        swath_m = float(command.get("swath_m", SAR_SWATH_M))
        altitude_m = float(command.get("altitude_m", SAR_ALTITUDE_M))
        if lat is None or lon is None:
            print("[SITL][SAR] search_grid command missing lat/lon")
            return
        print(f"[SITL][SAR] Launching search grid at ({lat}, {lon}), {grid_size_m}m grid")
        ok = _sar_missions.execute_search_grid_streaming(
            master,
            float(lat), float(lon),
            grid_size_m, swath_m, altitude_m,
            include_takeoff=True,
            takeoff_altitude_m=SAR_TAKEOFF_ALT_M,
            climb_speed_ms=SAR_CLIMB_SPEED_MS,
            arrival_radius_m=10.0,
            stop_event=stop_event,
            telemetry_callback=telemetry_callback,
        )
        print(f"[SITL][SAR] Search grid mission (streaming) {'COMPLETE' if ok else 'FAILED'}")

    elif cmd_type == "mob":
        track_points = command.get("track_points", [])
        corridor_half_width_m = float(command.get("corridor_half_width_m", SAR_CORRIDOR_HALF_WIDTH_M))
        swath_m = float(command.get("swath_m", SAR_SWATH_M))
        altitude_m = float(command.get("altitude_m", SAR_ALTITUDE_M))
        takeoff_altitude_m = float(command.get("takeoff_altitude_m", SAR_TAKEOFF_ALT_M))
        climb_speed_ms = float(command.get("climb_speed_ms", SAR_CLIMB_SPEED_MS))
        if len(track_points) < 2:
            print(f"[SITL][SAR] MOB command needs at least 2 track points, got {len(track_points)}")
            return
        print(f"[SITL][SAR] MAN OVERBOARD — launching search on {len(track_points)}-point track")
        ok = _sar_missions.execute_mob_search_streaming(
            master, track_points,
            corridor_half_width_m=corridor_half_width_m,
            swath_m=swath_m,
            altitude_m=altitude_m,
            takeoff_altitude_m=takeoff_altitude_m,
            climb_speed_ms=climb_speed_ms,
            include_takeoff=True,
            arrival_radius_m=10.0,
            stop_event=stop_event,
            telemetry_callback=telemetry_callback,
        )
        print(f"[SITL][SAR] MOB search mission (streaming) {'COMPLETE' if ok else 'FAILED'}")


def _handle_sitl_command(master: Any, cmd_payload: dict[str, Any]) -> None:
    """Blocking: translate a ground-station command into MAVLink and send it."""
    if _mavutil is None:
        return
    command = cmd_payload.get("command", {})
    cmd_type = command.get("type")
    source = cmd_payload.get("source")

    if cmd_type == "waypoint":
        target = command.get("target", {})
        lat = target.get("latitude")
        lon = target.get("longitude")
        alt = float(target.get("altitude") or 30.0)
        if lat is not None and lon is not None:
            # RTB-follow emits frequent waypoint updates; avoid repeated mode/arm
            # chatter so telemetry processing stays responsive.
            if source != "rtb_follow":
                # Fire-and-forget: set GUIDED mode then arm without waiting for ACKs
                # so the IO thread is never stalled over a radio link. ArduPilot
                # processes MAVLink messages in order, so the position target
                # arrives after mode/arm are applied.
                mode_mapping = master.mode_mapping()
                if mode_mapping and "GUIDED" in mode_mapping:
                    master.mav.set_mode_send(
                        master.target_system,
                        _mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                        mode_mapping["GUIDED"],
                    )
                    time.sleep(0.1)
                master.mav.command_long_send(
                    master.target_system,
                    master.target_component,
                    _mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                    0, 1, 0, 0, 0, 0, 0, 0,
                )
                time.sleep(0.1)
            master.mav.set_position_target_global_int_send(
                0,
                master.target_system,
                master.target_component,
                _mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                int(0b110111111000),
                int(float(lat) * 1e7),
                int(float(lon) * 1e7),
                alt,
                0, 0, 0,
                0, 0, 0,
                0, 0,
            )

    elif cmd_type == "rtb_follow":
        target = command.get("target", {})
        lat = target.get("latitude")
        lon = target.get("longitude")
        if lat is not None and lon is not None:
            vehicle_id = str(cmd_payload.get("vehicle_id") or "")
            now = time.monotonic()
            if now - _sitl_follow_guided_requests.get(vehicle_id, 0.0) >= 5.0:
                mode_mapping = master.mode_mapping()
                if mode_mapping and "GUIDED" in mode_mapping:
                    master.mav.set_mode_send(
                        master.target_system,
                        _mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                        mode_mapping["GUIDED"],
                    )
                    _sitl_follow_guided_requests[vehicle_id] = now
            master.mav.set_position_target_global_int_send(
                0,
                master.target_system,
                master.target_component,
                _mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                0b100111000000,
                int(float(lat) * 1e7),
                int(float(lon) * 1e7),
                float(target.get("altitude") or 30.0),
                float(command.get("velocity_north_ms") or 0.0),
                float(command.get("velocity_east_ms") or 0.0),
                0.0,
                0.0,
                0.0,
                0.0,
                math.radians(float(command.get("heading") or 0.0)),
                0.0,
            )

    elif cmd_type == "mission_plan":
        if _sar_missions is None:
            print("[SITL] mission_plan ignored: sar_missions helpers unavailable")
            return

        waypoints = command.get("waypoints") or []
        if not isinstance(waypoints, list) or len(waypoints) == 0:
            print("[SITL] mission_plan ignored: no waypoints provided")
            return

        mission_items = _sar_missions.build_mission_items(
            waypoints,
            force_guided_on_complete=bool(command.get("force_guided_on_complete", False)),
        )
        if not mission_items:
            print("[SITL] mission_plan ignored: no valid waypoint entries")
            return

        if not _sar_missions.upload_mission(master, mission_items):
            print("[SITL] mission_plan upload failed")
            return

        if bool(command.get("auto_arm_start", True)):
            _sar_missions.set_mode(master, "AUTO", wait_for_ack=False)
            time.sleep(0.2)
            _sar_missions.arm_vehicle(master)
            time.sleep(0.2)
            _sar_missions.start_mission(master)

    elif cmd_type == "set_mode":
        mode = command.get("mode")
        if not mode:
            print("[SITL] set_mode ignored: no mode specified")
            return
        if _sar_missions is None:
            print("[SITL] set_mode ignored: sar_missions helpers unavailable")
            return
        _sar_missions.set_mode(master, str(mode), wait_for_ack=False)

    elif cmd_type == "disarm":
        master.mav.command_long_send(
            master.target_system,
            master.target_component,
            _mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0,
            0,
            21196,
            0, 0, 0, 0, 0,
        )

    elif cmd_type == "rtcm_data":
        flags = command.get("flags", 0)
        data_len = command.get("len", 0)
        raw_data = command.get("data", [])
        if data_len > 0:
            padded_data = bytearray(raw_data + [0] * (180 - len(raw_data)))
            try:
                master.mav.gps_rtcm_data_send(flags, data_len, padded_data)
            except Exception as e:
                print(f"[SITL] RTCM send error: {e}")


@app.get("/api/vehicles")
async def get_vehicles() -> dict[str, Any]:
    """Return the public snapshot of every known vehicle."""
    async with state_lock:
        return {"vehicles": [public_vehicle(vehicle) for vehicle in vehicles.values()]}


@app.get("/api/video/streams")
async def list_video_streams(include_sources: bool = Query(False)) -> dict[str, Any]:
    """List configured video streams, optionally including source RTSP URLs."""
    streams: list[dict[str, Any]] = []
    for entry in video_streams.values():
        streams.append(dict(entry) if include_sources else public_video_stream(entry))
    return {"streams": streams}


@app.put("/api/video/streams/{vehicle_id}")
async def put_video_stream(vehicle_id: str, payload: dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """Create or update a vehicle's video stream configuration."""
    authorization_error = require_permission(authorization, "manage_video_streams")
    if authorization_error:
        return authorization_error
    vehicle_id = vehicle_id.strip()
    if not vehicle_id:
        return JSONResponse({"error": "vehicle_id is required"}, status_code=400)

    source_rtsp_url = payload.get("source_rtsp_url")
    playback_url = payload.get("playback_url")
    if source_rtsp_url is None and playback_url is None and vehicle_id not in video_streams:
        return JSONResponse(
            {"error": "Provide source_rtsp_url and/or playback_url when creating a stream"},
            status_code=400,
        )

    entry = upsert_video_stream(vehicle_id, payload)
    await broadcast_ui({"op": "video_stream_update", "video": public_video_stream(entry)})
    return JSONResponse({"ok": True, "stream": public_video_stream(entry)})


@app.delete("/api/video/streams/{vehicle_id}")
async def delete_video_stream(vehicle_id: str, authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """Remove a vehicle's video stream configuration."""
    authorization_error = require_permission(authorization, "manage_video_streams")
    if authorization_error:
        return authorization_error
    if vehicle_id not in video_streams:
        return JSONResponse({"error": "stream not found"}, status_code=404)
    video_streams.pop(vehicle_id, None)
    await broadcast_ui({"op": "video_stream_removed", "vehicle_id": vehicle_id})
    return JSONResponse({"ok": True})


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    """Return the current server-wide runtime settings."""
    return {**settings, "yp_role_vehicle_id": _yp_role_vehicle_id}


@app.get("/api/rtcm/status")
async def get_rtcm_status() -> dict[str, Any]:
    """Return the live connection status of the RTCM correction stream."""
    return dict(rtcm_status)


def _parse_log_time(value: str, name: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _log_range(start: Optional[str], end: Optional[str], last_hours: Optional[float]) -> tuple[datetime, datetime]:
    """Validate an explicit or relative export time range."""
    if last_hours is not None:
        if start or end:
            raise ValueError("use either last_hours or start/end, not both")
        if not math.isfinite(last_hours) or last_hours <= 0 or last_hours > 30 * 24:
            raise ValueError("last_hours must be greater than zero and no more than 30 days")
        end_time = datetime.now(timezone.utc)
        return end_time - timedelta(hours=last_hours), end_time
    if not start or not end:
        raise ValueError("start and end timestamps are required")
    start_time = _parse_log_time(start, "start")
    end_time = _parse_log_time(end, "end")
    if start_time >= end_time:
        raise ValueError("start must be before end")
    if end_time > datetime.now(timezone.utc) + timedelta(minutes=1):
        raise ValueError("end cannot be in the future")
    if end_time - start_time > timedelta(days=30):
        raise ValueError("the export range cannot exceed 30 days")
    return start_time, end_time


def _format_log_time(value: datetime) -> str:
    """Format a timestamp consistently for exported log records."""
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _influx_log_record(record: Any) -> dict[str, Any]:
    """Convert a pivoted Influx record to the public JSONL record shape."""
    values = record.values
    fields = {
        key: value
        for key, value in values.items()
        if key not in {"result", "table", "_start", "_stop", "_time", "_measurement", "vehicle_id", "vehicle_type", "topic", "msg_type"}
    }
    return {
        "timestamp": _format_log_time(record.get_time()),
        "vehicle_id": values.get("vehicle_id", "unknown"),
        "vehicle_type": values.get("vehicle_type", "unknown"),
        "fields": fields,
    }


def _is_heartbeat_record(record: Any) -> bool:
    """Return whether an Influx record represents a heartbeat message."""
    message_type = str(record.values.get("msg_type", ""))
    topic = str(record.values.get("topic", ""))
    return message_type.lower().endswith("heartbeat") or topic.lower().rstrip("/").endswith("/heartbeat")


def _query_log_records(start: datetime, end: datetime) -> Any:
    """Create a streaming query for the retained yp_messages measurement."""
    if not query_api:
        raise RuntimeError("InfluxDB is unavailable")
    start_value = _format_log_time(start)
    end_value = _format_log_time(end)
    flux = f'''from(bucket: "{INFLUX_BUCKET}")
  |> range(start: time(v: "{start_value}"), stop: time(v: "{end_value}"))
  |> filter(fn: (r) => r._measurement == "yp_messages")
  |> pivot(rowKey: ["_time", "vehicle_id", "vehicle_type", "topic", "msg_type"], columnKey: ["_field"], valueColumn: "_value")'''
    return query_api.query_stream(query=flux, org=INFLUX_ORG)


@app.get("/api/logs/export")
async def export_log(
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    last_hours: Optional[float] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
) -> Response:
    """Stream retained yp_messages data as a JSON Lines flight log."""
    authorization_error = require_permission(authorization, "manage_settings")
    if authorization_error:
        return authorization_error
    try:
        start_time, end_time = _log_range(start, end, last_hours)
        records = (record for record in _query_log_records(start_time, end_time) if not _is_heartbeat_record(record))
        first_record = next(records)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except StopIteration:
        return JSONResponse({"error": "No retained log data exists in the requested range"}, status_code=404)
    except Exception as error:
        print(f"Flight log export failed: {error}")
        return JSONResponse({"error": "Unable to query retained flight log data"}, status_code=503)

    metadata = {
        "format": "yp-ground-station-log",
        "schema_version": 1,
        "exported_at": _format_log_time(datetime.now(timezone.utc)),
        "start": _format_log_time(start_time),
        "end": _format_log_time(end_time),
        "bucket": INFLUX_BUCKET,
        "measurement": "yp_messages",
    }

    def lines() -> Any:
        compressor = zlib.compressobj(wbits=31)

        def compress(line: str) -> bytes:
            return compressor.compress(line.encode("utf-8"))

        yield compress(json.dumps(metadata, separators=(",", ":")) + "\n")
        yield compress(json.dumps(_influx_log_record(first_record), separators=(",", ":"), default=str) + "\n")
        for record in records:
            yield compress(json.dumps(_influx_log_record(record), separators=(",", ":"), default=str) + "\n")
        yield compressor.flush()

    filename = f"yp-flight-log-{start_time.strftime('%Y%m%dT%H%M%SZ')}-{end_time.strftime('%Y%m%dT%H%M%SZ')}.jsonl.gz"
    return StreamingResponse(
        lines(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.put("/api/settings")
async def update_settings(payload: dict[str, Any], authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """Validate, apply, and persist Settings modal values."""
    authorization_error = require_permission(authorization, "manage_settings")
    if authorization_error:
        return authorization_error
    
    application_payload = {key: value for key, value in payload.items() if key in APPLICATION_SETTING_DEFAULTS}
    if not application_payload:
        return JSONResponse({"error": "At least one setting value is required"}, status_code=400)

    success, message = update_application_settings(application_payload)
    if not success:
        return JSONResponse({"error": message}, status_code=400)

    persisted_settings = get_application_settings()
    settings.update(persisted_settings)
    global _yp_role_vehicle_id
    _yp_role_vehicle_id = persisted_settings["yp_role_vehicle_id"]
    return JSONResponse(settings)


def _apply_deconfliction_settings(values: dict[str, Any]) -> None:
    deconfliction_engine.set_enabled(values.get("enabled", False))
    deconfliction_engine.global_radius_m = values.get("global_radius_m", 10.0)
    deconfliction_engine.radius_per_type = DEFAULT_DECONFLICT_RADIUS_M.copy()
    # Older databases could contain overrides that the API now rejects.
    for vehicle_type, value in values.get("radius_per_type", {}).items():
        try:
            radius = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if not isinstance(value, bool) and math.isfinite(radius) and radius > 0:
            deconfliction_engine.set_radius(vehicle_type, radius)


@app.get("/api/deconfliction/settings")
async def get_deconfliction_settings_api() -> dict[str, Any]:
    """Get current deconfliction settings."""
    return get_deconfliction_settings()


@app.put("/api/deconfliction/settings")
async def update_deconfliction_settings_api(
    payload: dict[str, Any],
    authorization: Optional[str] = Header(default=None)
) -> JSONResponse:
    """Update deconfliction settings. Requires manage_settings permission."""
    authorization_error = require_permission(authorization, "manage_settings")
    if authorization_error:
        return authorization_error
    
    success, message = update_deconfliction_settings(payload)
    if success:
        # Reload deconfliction engine settings
        async with _deconfliction_lock:
            db_settings = get_deconfliction_settings()
            _apply_deconfliction_settings(db_settings)
        
        return JSONResponse(db_settings)
    else:
        return JSONResponse({"error": message}, status_code=400)


@app.get("/api/deconfliction/conflicts")
async def get_deconfliction_conflicts() -> dict[str, Any]:
    """Get current vehicle conflicts detected by deconfliction engine."""
    async with _deconfliction_lock:
        conflicts = deconfliction_engine.detect_conflicts()
    
    return {
        "enabled": deconfliction_engine.enabled,
        "conflicts": [
            {
                "low_priority_vehicle": c[0],
                "high_priority_vehicle": c[1],
            }
            for c in conflicts
        ],
    }


# ---------------------------------------------------------------------------
# YP role assignment endpoints
# ---------------------------------------------------------------------------

@app.get("/api/yp/role")
async def get_yp_role() -> dict[str, Any]:
    """Return the vehicle currently designated to act as the YP (mother vessel)."""
    return {"vehicle_id": _yp_role_vehicle_id}


@app.post("/api/yp/role")
async def set_yp_role(payload: dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """
    Designate a connected vehicle as the YP (mother vessel).

    Body: { "vehicle_id": "blueboat" }  — assign a vehicle.
           { "vehicle_id": null }        — clear the assignment.

    The designated vehicle's type will be overridden to ``"yp"`` for all
    subsequent telemetry.  Clearing the assignment reverts the vehicle to
    its natural type on its next incoming message.
    """
    global _yp_role_vehicle_id

    authorization_error = require_permission(authorization, "manage_settings")
    if authorization_error:
        return authorization_error

    raw = payload.get("vehicle_id")
    new_role_id: Optional[str] = str(raw).strip() if raw and str(raw).strip() else None
    success, message = update_application_settings({"yp_role_vehicle_id": new_role_id})
    if not success:
        return JSONResponse({"error": message}, status_code=400)
    old_role_id = _yp_role_vehicle_id
    _yp_role_vehicle_id = new_role_id

    async with state_lock:
        # Promote new role vehicle to "yp" immediately (don't wait for next message)
        if new_role_id and new_role_id in vehicles:
            vehicles[new_role_id]["vehicle_type"] = "yp"
            snap = {k: v for k, v in public_vehicle(vehicles[new_role_id]).items() if k != "history"}
            await broadcast_ui({"op": "vehicle_update", "vehicle": snap})

        # Revert previous role vehicle to its stored natural type
        if old_role_id and old_role_id != new_role_id and old_role_id in vehicles:
            old_v = vehicles[old_role_id]
            old_v["vehicle_type"] = old_v.get("_natural_type") or infer_vehicle_type(old_role_id)
            snap = {k: v for k, v in public_vehicle(old_v).items() if k != "history"}
            await broadcast_ui({"op": "vehicle_update", "vehicle": snap})

    return JSONResponse({"ok": True, "vehicle_id": _yp_role_vehicle_id})


@app.get("/api/vehicles/{vehicle_id}")
async def get_vehicle(vehicle_id: str) -> JSONResponse:
    """Return the public snapshot of a single vehicle."""
    async with state_lock:
        vehicle = vehicles.get(vehicle_id)
    if not vehicle:
        return JSONResponse({"error": "vehicle not found"}, status_code=404)
    return JSONResponse(public_vehicle(vehicle))


def _select_yp_vehicle_locked() -> Optional[dict[str, Any]]:
    """Pick the mother-vessel source while holding ``state_lock``.

    Priority:
    1) Explicit YP-role assignment via /api/yp/role.
    2) Any vehicle currently typed as "yp" (deterministic fallback).
    """
    if _yp_role_vehicle_id:
        role_vehicle = vehicles.get(_yp_role_vehicle_id)
        if role_vehicle is not None:
            return role_vehicle

    yp_candidates = [v for v in vehicles.values() if v.get("vehicle_type") == "yp"]
    if not yp_candidates:
        return None

    # Prefer connected, non-sim vehicles first for operational behavior.
    return min(
        yp_candidates,
        key=lambda v: (
            0 if v.get("connected") else 1,
            0 if not str(v.get("vehicle_id") or "").startswith("sim-") else 1,
            str(v.get("vehicle_id") or ""),
        ),
    )


@app.post("/api/sar/mob")
async def trigger_mob(payload: dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """
    Trigger a Man Overboard search mission.

    Reads the YP vessel's position history to build track points, selects the
    first available non-YP connected vehicle, and routes a 'mob' command to its
    bridge via the existing vehicle WebSocket queue.

    Optional body: { "vehicle_id": "uav-001" } to target a specific vehicle.
    """
    authorization_error = require_permission(authorization, "trigger_mob")
    if authorization_error:
        return authorization_error

    track_window_s = float(settings.get("mob_track_seconds", SAR_MOB_TRACK_SECONDS))
    if payload and payload.get("track_seconds") is not None:
        try:
            track_window_s = float(payload.get("track_seconds"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "track_seconds must be a number"}, status_code=400)
        if track_window_s <= 0:
            return JSONResponse({"error": "track_seconds must be > 0"}, status_code=400)

    async with state_lock:
        # Find YP (ship) vehicle and extract track points from its position history.
        # Always honor explicit role assignment first.
        yp_vehicle = _select_yp_vehicle_locked()
        if not yp_vehicle:
            return JSONResponse({"error": "No YP vessel tracked"}, status_code=404)

        yp_history = list(yp_vehicle.get("history", []))
        if len(yp_history) < 2:
            return JSONResponse(
                {"error": "Insufficient YP track history for MOB search (need at least 2 position fixes)"},
                status_code=409,
            )

        latest_stamp = float(yp_history[-1].get("stamp") or time.time())
        cutoff_stamp = latest_stamp - track_window_s
        windowed_history = [
            p for p in yp_history
            if float(p.get("stamp") or 0.0) >= cutoff_stamp
        ]
        if len(windowed_history) < 2:
            return JSONResponse(
                {
                    "error": (
                        f"Insufficient YP fixes in requested track window ({track_window_s:.0f}s). "
                        "Increase Track length or wait for more YP telemetry."
                    )
                },
                status_code=409,
            )

        track_points = [[p["latitude"], p["longitude"]] for p in windowed_history]

        # Resolve target vehicle
        requested_id: Optional[str] = payload.get("vehicle_id") if payload else None
        if requested_id:
            if requested_id not in vehicles:
                return JSONResponse({"error": f"Vehicle '{requested_id}' not found"}, status_code=404)
            if vehicles[requested_id].get("vehicle_type") == "ugv":
                return JSONResponse({"error": "UGVs cannot be dispatched for a Man Overboard search"}, status_code=422)
            target_vehicle_id = requested_id
        else:
            # Prefer UAV SITL bridges first, then any SITL bridge, then hardware
            # bridges (non-sim- prefix), then any non-YP vehicle as fallback.
            target = (
                # Tier 1 — UAV SITL bridge (best: real ArduPilot MAVLink execution)
                next(
                    (v for v in vehicles.values()
                     if v.get("vehicle_type") == "uav"
                     and v.get("connected")
                     and v["vehicle_id"] in sitl_bridges),
                    None,
                )
                # Tier 2 — any SITL bridge
                or next(
                    (v for v in vehicles.values()
                     if v.get("vehicle_type") not in ("yp", "ugv")
                     and v.get("connected")
                     and v["vehicle_id"] in sitl_bridges),
                    None,
                )
                # Tier 3 — hardware bridge (non-sim- prefix)
                or next(
                    (v for v in vehicles.values()
                     if v.get("vehicle_type") not in ("yp", "ugv")
                     and v.get("connected")
                     and not v["vehicle_id"].startswith("sim-")),
                    None,
                )
                # Tier 4 — sim UAV (visual-only fallback for testing)
                or next(
                    (v for v in vehicles.values()
                     if v.get("vehicle_type") == "uav"
                     and v.get("connected")
                     and v["vehicle_id"].startswith("sim-")),
                    None,
                )
                # Tier 5 — any connected non-YP non-UGV sim vehicle
                or next(
                    (v for v in vehicles.values()
                     if v.get("vehicle_type") not in ("yp", "ugv")
                     and v.get("connected")
                     and v["vehicle_id"].startswith("sim-")),
                    None,
                )
            )
            if not target:
                return JSONResponse(
                    {"error": "No available vehicle for MOB search. Connect a SITL bridge or ensure sim vehicles are running."},
                    status_code=409,
                )
            target_vehicle_id = target["vehicle_id"]

    def _get_float(name: str, default: float) -> float:
        value = payload.get(name) if payload else None
        if value is None:
            return default
        return float(value)

    mob_command: dict[str, Any] = {
        "type": "mob",
        "track_points": track_points,
        "corridor_half_width_m": _get_float("corridor_half_width_m", float(settings.get("mob_corridor_half_width_m", SAR_CORRIDOR_HALF_WIDTH_M))),
        "swath_m": _get_float("swath_m", float(settings.get("mob_swath_m", SAR_SWATH_M))),
        "altitude_m": _get_float("altitude_m", float(settings.get("mob_altitude_m", SAR_ALTITUDE_M))),
        "takeoff_altitude_m": _get_float("takeoff_altitude_m", float(settings.get("mob_takeoff_altitude_m", SAR_TAKEOFF_ALT_M))),
        "climb_speed_ms": _get_float("climb_speed_ms", float(settings.get("mob_climb_speed_ms", SAR_CLIMB_SPEED_MS))),
    }

    await route_command(target_vehicle_id, mob_command, source="sar_api")
    return JSONResponse({"ok": True, "vehicle_id": target_vehicle_id})


@app.websocket("/ws/vehicle/{vehicle_id}")
async def vehicle_ws(websocket: WebSocket, vehicle_id: str) -> None:
    """Bridge a single vehicle's telemetry (inbound) and command queue (outbound) over a WebSocket."""
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

    tasks = [asyncio.create_task(receive_loop()), asyncio.create_task(send_loop())]
    try:
        await asyncio.gather(*tasks)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A replacement connection may already own this vehicle's queue.
        if vehicle_queues.get(vehicle_id) is queue:
            vehicle_queues.pop(vehicle_id, None)
            async with state_lock:
                if vehicle_id in vehicles:
                    vehicles[vehicle_id]["connected"] = False
                    vehicles[vehicle_id]["last_seen_age"] = time.time() - vehicles[vehicle_id].get("last_seen", time.time())
            await broadcast_ui({"op": "vehicle_disconnected", "vehicle_id": vehicle_id})


@app.websocket("/ws/ui")
async def ui_ws(websocket: WebSocket, token: Optional[str] = None) -> None:
    """Serve the ground-station UI: validate JWT token, send initial snapshot, then relay commands with permission checks."""
    await websocket.accept()
    
    # Validate JWT token
    cookie_token = websocket.cookies.get("auth_token")
    user = get_current_user(token or cookie_token)
    if not user:
        await websocket.send_json({"error": "Authentication required. Please login."})
        await websocket.close(code=4001, reason="Unauthorized")
        return
    
    ui_connections.add(websocket)
    try:
        async with state_lock:
            await websocket.send_json({
                "op": "snapshot",
                "vehicles": [public_vehicle(vehicle) for vehicle in vehicles.values()],
                "waypoints": list(shared_waypoints.values()),
                "sar_patterns": shared_sar_patterns,
                "mission_plans": shared_mission_plans,
                "rtcm_status": dict(rtcm_status),
            })
        while True:
            payload = await websocket.receive_json()
            if payload.get("op") == "command":
                # Check permissions for the command type
                cmd_type = payload.get("command", {}).get("type")
                if not _check_command_permission(user, cmd_type):
                    await websocket.send_json({
                        "error": "Insufficient permissions for this command",
                        "command_type": cmd_type
                    })
                    continue
                await route_command(payload.get("vehicle_id"), payload.get("command", {}), source="ui")
    except WebSocketDisconnect:
        pass
    finally:
        ui_connections.discard(websocket)


def _check_command_permission(user: "User", cmd_type: Optional[str]) -> bool:
    """Check if a user has permission to execute a specific command type."""
    if not user or not user.active:
        return False
    
    # Map command types to required permissions
    command_permissions = {
        "waypoint": "send_waypoint",
        "rtb": "send_rtb",
        "set_mode": "set_vehicle_mode",
        "cancel_sar": "cancel_sar",
        "search_grid": "search_grid",
        "mob": "trigger_mob",
        "land_on_boat": "send_rtb",
        "clear_sar_pattern": "cancel_sar",
        "mission_plan": "upload_mission",
        "ship_relative_trajectory": "upload_mission",
        "trajectory": "send_waypoint",
    }
    
    required_permission = command_permissions.get(cmd_type)
    if not required_permission:
        return False
    
    return user.has_permission(required_permission)


@app.websocket("/ws/rosbridge")
async def rosbridge_ws(websocket: WebSocket) -> None:
    """Minimal rosbridge-protocol WebSocket for subscribe/publish/command ops."""
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
    """Update vehicle state from an incoming telemetry message and fan it out to clients."""
    now = float(payload.get("stamp") or time.time())
    vehicle_id = str(payload.get("vehicle_id") or topic_vehicle_id(payload.get("topic", "")) or "unknown")
    # Natural type from the message payload; stored so clearing the YP role can revert it.
    natural_type = normalize_vehicle_type(payload.get("vehicle_type") or infer_vehicle_type(vehicle_id))
    # Apply YP role override: designate this vehicle as the mother vessel.
    vehicle_type = "yp" if _yp_role_vehicle_id and vehicle_id == _yp_role_vehicle_id else natural_type
    topic = str(payload.get("topic") or f"/vehicles/{vehicle_id}/unknown")
    msg_type = str(payload.get("type") or payload.get("msg_type") or "unknown")
    msg = payload.get("msg", {})

    if msg_type == "yp_ground_station/MissionComplete":
        shared_mission_completion_targets.pop(vehicle_id, None)
        if shared_mission_plans.pop(vehicle_id, None) is not None:
            await broadcast_ui({"op": "mission_plan_cleared", "vehicle_id": vehicle_id})

    update: dict[str, Any] = {
        "vehicle_id": vehicle_id,
        "vehicle_type": vehicle_type,
        "topic": topic,
        "type": msg_type,
        "stamp": now,
        "msg": msg,
    }
    mission_completed = False

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
        vehicle["_natural_type"] = natural_type  # retained for YP role revert
        vehicle["connected"] = True
        vehicle["last_seen"] = now
        vehicle["last_seen_age"] = 0
        vehicle["messages"][topic] = {"type": msg_type, "stamp": now, "msg": msg}

        # Extract dynamic video streams from the payload ---
        if "video" in payload:
            vehicle["video"] = payload["video"]
        elif "video" in msg:
            vehicle["video"] = msg["video"]

        nav = extract_navsatfix(topic, msg_type, msg)
        if nav:
            vehicle["position"] = nav
            vehicle["history"].append({"stamp": now, **nav})
            completion_target = shared_mission_completion_targets.get(vehicle_id)
            if completion_target and _haversine_m(
                nav["latitude"], nav["longitude"], completion_target["latitude"], completion_target["longitude"],
            ) <= completion_target["arrival_radius_m"]:
                shared_mission_completion_targets.pop(vehicle_id, None)
                if shared_mission_plans.pop(vehicle_id, None) is not None:
                    mission_completed = True
            
            # Update deconfliction engine with position
            if deconfliction_engine.enabled:
                async with _deconfliction_lock:
                    deconfliction_engine.update_vehicle(
                        vehicle_id,
                        vehicle.get("vehicle_type", "uav"),
                        nav,
                    )

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
        # Strip history from the per-message update — it grows to thousands of entries
        # and would otherwise be serialised and sent to the UI 75+ times per second.
        # The initial /ws/ui snapshot sends the full history; clients accumulate
        # subsequent positions locally from the NavSatFix messages.
        slim_snapshot = {k: v for k, v in vehicle_snapshot.items() if k != "history"}

    if mission_completed:
        await broadcast_ui({"op": "mission_plan_cleared", "vehicle_id": vehicle_id})
    write_influx(update)
    await broadcast_ui({"op": "vehicle_update", "vehicle": slim_snapshot, "message": update})
    await broadcast_ros(topic, msg, msg_type)


def _compute_sar_waypoints(command: dict[str, Any]) -> list[list[float]]:
    """Compute a SAR path once for both UI overlays and simulated navigation."""
    if _sar_missions is None:
        return []
    cmd_type = command.get("type")
    try:
        if cmd_type == "search_grid":
            lat = command.get("lat")
            lon = command.get("lon")
            if lat is None or lon is None:
                return []
            wps = _sar_missions.calculate_search_grid_waypoints(
                float(lat), float(lon),
                float(command.get("grid_size_m", 200)),
                float(command.get("swath_m", SAR_SWATH_M)),
                float(command.get("altitude_m", SAR_ALTITUDE_M)),
            )
        elif cmd_type == "mob":
            track_points = command.get("track_points", [])
            if len(track_points) < 2:
                return []
            wps = _sar_missions.calculate_mob_waypoints(
                track_points,
                float(command.get("corridor_half_width_m", SAR_CORRIDOR_HALF_WIDTH_M)),
                float(command.get("swath_m", SAR_SWATH_M)),
                float(command.get("altitude_m", SAR_ALTITUDE_M)),
            )
        else:
            return []
        return [[float(wp[0]), float(wp[1]), float(wp[2])] for wp in wps]
    except Exception as exc:
        print(f"[SAR] Pattern compute error: {exc}")
        return []


async def route_command(vehicle_id: Optional[str], command: dict[str, Any], source: str) -> None:
    """Route an operator command to a vehicle, handling RTB-follow and SAR pattern broadcast specially."""
    if not vehicle_id:
        return

    cmd_type = command.get("type")
    is_temporary_avoidance = source == "deconfliction"

    # A new operational task supersedes a previously published mission route.
    # Re-uploading a mission replaces it below with the new route instead.
    if not is_temporary_avoidance and cmd_type != "mission_plan":
        shared_mission_completion_targets.pop(vehicle_id, None)
        if shared_mission_plans.pop(vehicle_id, None) is not None:
            await broadcast_ui({"op": "mission_plan_cleared", "vehicle_id": vehicle_id})

    if not is_temporary_avoidance and cmd_type == "waypoint":
        target = command.get("target", {})
        lat, lon = target.get("latitude"), target.get("longitude")
        if lat is not None and lon is not None:
            waypoint = {
                "vehicle_id": vehicle_id,
                "latitude": float(lat),
                "longitude": float(lon),
            }
            shared_waypoints[vehicle_id] = waypoint
            await broadcast_ui({"op": "waypoint_overlay", "waypoint": waypoint})

    if not is_temporary_avoidance and cmd_type in ("rtb", "cancel_sar", "waypoint"):
        if shared_sar_patterns.pop(vehicle_id, None) is not None:
            await broadcast_ui({"op": "sar_pattern_cleared", "vehicle_id": vehicle_id})

    if cmd_type == "clear_sar_pattern":
        if shared_sar_patterns.pop(vehicle_id, None) is not None:
            await broadcast_ui({"op": "sar_pattern_cleared", "vehicle_id": vehicle_id})
        await _emit_command_ack(vehicle_id, command, source)
        return

    # Any operator command except RTB or Land should terminate active RTB-follow / Landing
    if source not in ("rtb_follow", "land_on_boat") and cmd_type not in ("rtb", "land_on_boat"):
        await _stop_rtb_follow(vehicle_id)
        await _stop_land_on_boat(vehicle_id)

    if cmd_type == "rtb":
        if vehicle_id in vehicles:
            await _update_deconfliction_state(vehicle_id, vehicles[vehicle_id], command)
        await _start_rtb_follow(vehicle_id, source)
        await _emit_command_ack(vehicle_id, command, source)
        return

    # HANDLER FOR LAND COMMANDS
    if cmd_type == "land_on_boat":
        await _start_land_on_boat(vehicle_id, source)
        await _emit_command_ack(vehicle_id, command, source)
        return

    # Broadcast SAR flight-path pattern to the UI before dispatching
    if cmd_type in ("search_grid", "mob"):
        sar_waypoints = _compute_sar_waypoints(command)
        if sar_waypoints:
            pattern = {
                "pattern_type": cmd_type,
                "waypoints": [waypoint[:2] for waypoint in sar_waypoints],
            }
            shared_sar_patterns[vehicle_id] = pattern
            await broadcast_ui({
                "op": "sar_pattern",
                "vehicle_id": vehicle_id,
                **pattern,
            })

        # For websocket sim vehicles only, embed full 3-D waypoints so
        # sim_vehicle.py can navigate the pattern visually. Do not attach this
        # list for hardware bridges.
        if vehicle_id.startswith("sim-") and sar_waypoints:
            command = {**command, "sim_waypoints": sar_waypoints}

    if cmd_type == "mission_plan":
        mission_points = [
            [float(waypoint["latitude"]), float(waypoint["longitude"])]
            for waypoint in command.get("waypoints", [])
            if isinstance(waypoint, dict)
            and waypoint.get("latitude") is not None
            and waypoint.get("longitude") is not None
        ]
        if mission_points:
            shared_mission_plans[vehicle_id] = mission_points
            final_waypoint = next(
                waypoint
                for waypoint in reversed(command.get("waypoints", []))
                if isinstance(waypoint, dict)
                and waypoint.get("latitude") is not None
                and waypoint.get("longitude") is not None
            )
            shared_mission_completion_targets[vehicle_id] = {
                "latitude": mission_points[-1][0],
                "longitude": mission_points[-1][1],
                "arrival_radius_m": max(
                    1.0,
                    float(final_waypoint.get("acceptance_radius_m", MISSION_ARRIVAL_RADIUS_M)),
                ),
            }
            await broadcast_ui({
                "op": "mission_plan_overlay",
                "vehicle_id": vehicle_id,
                "waypoints": mission_points,
            })

    # Update deconfliction engine with the command
    if vehicle_id in vehicles and not is_temporary_avoidance:
        await _update_deconfliction_state(vehicle_id, vehicles[vehicle_id], command)

    await _dispatch_vehicle_command(vehicle_id, command, source, emit_ack=True, write_log=True)


async def _dispatch_vehicle_command(
    vehicle_id: str,
    command: dict[str, Any],
    source: str,
    *,
    emit_ack: bool,
    write_log: bool,
) -> dict[str, Any]:
    """Queue a command for delivery to a vehicle, optionally logging and acking it."""
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

    if write_log:
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
        await broadcast_ros(f"/vehicles/{vehicle_id}/commands", command, "yp_ground_station/Command")

    if emit_ack:
        ack_payload = dict(payload)
        ack_payload["op"] = "command_ack"
        await broadcast_ui(ack_payload)

    return payload


async def _emit_command_ack(vehicle_id: str, command: dict[str, Any], source: str) -> None:
    """Log a command and broadcast a command_ack event without queuing delivery."""
    payload = {
        "op": "command_ack",
        "vehicle_id": vehicle_id,
        "source": source,
        "stamp": time.time(),
        "command": command,
        "delivered": vehicle_id in vehicle_queues,
    }
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
    await broadcast_ui(payload)
    await broadcast_ros(f"/vehicles/{vehicle_id}/commands", command, "yp_ground_station/Command")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in meters between two lat/lon points."""
    la1, la2 = math.radians(lat1), math.radians(lat2)
    dlo = math.radians(lon2 - lon1)
    dlat = la2 - la1
    a = (math.sin(dlat / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2)
    return EARTH_RADIUS_M * 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _destination_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """Return the lat/lon reached by traveling distance_m meters along bearing_deg from a point."""
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    bearing_r = math.radians(bearing_deg)
    d = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(
        math.sin(lat_r) * math.cos(d)
        + math.cos(lat_r) * math.sin(d) * math.cos(bearing_r)
    )
    lon2 = lon_r + math.atan2(
        math.sin(bearing_r) * math.sin(d) * math.cos(lat_r),
        math.cos(d) - math.sin(lat_r) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the initial bearing from one geographic point to another."""
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    dlon_r = math.radians(lon2 - lon1)
    y = math.sin(dlon_r) * math.cos(lat2_r)
    x = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon_r)
    return math.degrees(math.atan2(y, x)) % 360.0


async def _stop_rtb_follow(vehicle_id: str) -> None:
    """Cancel and await a vehicle's running RTB-follow task, if any."""
    task = _rtb_follow_tasks.pop(vehicle_id, None)
    if not task:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[RTB] Stop task error for {vehicle_id}: {exc}")


async def _start_rtb_follow(vehicle_id: str, source: str) -> None:
    """Replace any existing RTB-follow task for a vehicle and start a new one."""
    await _stop_rtb_follow(vehicle_id)
    task = asyncio.create_task(_rtb_follow_loop(vehicle_id), name=f"rtb-follow-{vehicle_id}")
    _rtb_follow_tasks[vehicle_id] = task
    print(f"[RTB] Started return-to-boat follow for {vehicle_id} (source={source})")


async def _rtb_follow_loop(vehicle_id: str) -> None:
    """Continuously steer a vehicle to a moving point directly aft of the YP."""
    approach_side: Optional[int] = None
    approach_stage = 0
    try:
        while True:
            rtb_update_hz = float(settings.get("rtb_update_hz") or RTB_UPDATE_HZ)
            period_s = 1.0 / max(rtb_update_hz, 0.2)

            async with state_lock:
                target_vehicle = vehicles.get(vehicle_id)
                yp_vehicle = _select_yp_vehicle_locked()

                if not target_vehicle or not target_vehicle.get("connected"):
                    print(f"[RTB] Target vehicle {vehicle_id} unavailable; stopping follow")
                    return

                target_pos = target_vehicle.get("position") or {}
                yp_pos = (yp_vehicle or {}).get("position") or {}
                if yp_pos.get("latitude") is None or yp_pos.get("longitude") is None:
                    # No YP fix yet; keep trying.
                    target_snapshot = None
                else:
                    yp_heading = float((yp_vehicle or {}).get("heading") or 0.0) % 360.0
                    stern_lat, stern_lon = _destination_point(
                        float(yp_pos["latitude"]),
                        float(yp_pos["longitude"]),
                        yp_heading + 180.0,
                        float(settings.get("rtb_stern_distance_m", RTB_STERN_DISTANCE_M)),
                    )
                    vehicle_lat = target_pos.get("latitude")
                    vehicle_lon = target_pos.get("longitude")
                    approach_lat, approach_lon = stern_lat, stern_lon
                    follow_heading = yp_heading
                    if vehicle_lat is not None and vehicle_lon is not None:
                        relative_bearing = (
                            _bearing_deg(
                                float(yp_pos["latitude"]),
                                float(yp_pos["longitude"]),
                                float(vehicle_lat),
                                float(vehicle_lon),
                            ) - yp_heading
                        ) % 360.0
                        if approach_side is None and not 165.0 <= relative_bearing <= 195.0:
                            approach_side = -1 if relative_bearing < 180.0 else 1
                            approach_stage = 1
                        if approach_side is not None:
                            safety_radius = (
                                deconfliction_engine.get_radius(target_vehicle.get("vehicle_type", "uav"))
                                + deconfliction_engine.get_radius("yp")
                            )
                            route_distance = max(
                                float(settings.get("rtb_stern_distance_m", RTB_STERN_DISTANCE_M)) + 10.0,
                                (safety_radius * 2.0) + 10.0,
                            )
                            if approach_stage == 1:
                                approach_bearing = (yp_heading + (90.0 if approach_side < 0 else 270.0)) % 360.0
                                approach_distance = route_distance
                            else:
                                approach_bearing = (yp_heading + 180.0) % 360.0
                                approach_distance = route_distance
                            approach_lat, approach_lon = _destination_point(
                                float(yp_pos["latitude"]),
                                float(yp_pos["longitude"]),
                                approach_bearing,
                                approach_distance,
                            )
                            follow_heading = _bearing_deg(
                                float(vehicle_lat), float(vehicle_lon), approach_lat, approach_lon,
                            )
                            approach_tolerance = max(5.0, min(10.0, route_distance * 0.2))
                            if approach_stage == 1 and _haversine_m(
                                float(vehicle_lat), float(vehicle_lon), approach_lat, approach_lon,
                            ) <= approach_tolerance:
                                approach_stage = 2
                            elif approach_stage == 2 and _haversine_m(
                                float(vehicle_lat), float(vehicle_lon), approach_lat, approach_lon,
                            ) <= approach_tolerance:
                                approach_side = None
                    # Use the configured RTB transit altitude rather than the vehicle's
                    # live altitude; re-sampling live altitude each cycle would let any
                    # small descent become the new setpoint, causing drift.
                    target_alt = float(settings.get("rtb_altitude_m", RTB_ALTITUDE_M))
                    yp_speed_mps = 0.0
                    yp_history = (yp_vehicle or {}).get("history") or []
                    if len(yp_history) >= 2:
                        previous_fix = yp_history[-2]
                        latest_fix = yp_history[-1]
                        elapsed_s = float(latest_fix.get("stamp") or 0.0) - float(previous_fix.get("stamp") or 0.0)
                        if elapsed_s > 0:
                            yp_speed_mps = min(
                                25.0,
                                _haversine_m(
                                    float(previous_fix.get("latitude", 0.0)),
                                    float(previous_fix.get("longitude", 0.0)),
                                    float(latest_fix.get("latitude", 0.0)),
                                    float(latest_fix.get("longitude", 0.0)),
                                ) / elapsed_s,
                            )
                    target_snapshot = {
                        "lat": approach_lat,
                        "lon": approach_lon,
                        "alt": target_alt,
                        "veh_lat": float(target_pos.get("latitude")) if target_pos.get("latitude") is not None else None,
                        "veh_lon": float(target_pos.get("longitude")) if target_pos.get("longitude") is not None else None,
                        "yp_heading": yp_heading,
                        "follow_heading": follow_heading,
                        "yp_speed_mps": yp_speed_mps,
                        "yp_velocity_north_ms": yp_speed_mps * math.cos(math.radians(yp_heading)),
                        "yp_velocity_east_ms": yp_speed_mps * math.sin(math.radians(yp_heading)),
                    }

            if target_snapshot is None:
                await asyncio.sleep(period_s)
                continue

            follow_command = {
                "type": "rtb_follow" if approach_side is None else "waypoint",
                "target": {
                    "latitude": target_snapshot["lat"],
                    "longitude": target_snapshot["lon"],
                    "altitude": target_snapshot["alt"],
                },
            }
            if follow_command["type"] == "rtb_follow":
                follow_command["heading"] = target_snapshot["yp_heading"]
                follow_command["speed_mps"] = target_snapshot["yp_speed_mps"]
                follow_command["velocity_north_ms"] = target_snapshot["yp_velocity_north_ms"]
                follow_command["velocity_east_ms"] = target_snapshot["yp_velocity_east_ms"]
            else:
                follow_command["heading"] = target_snapshot["follow_heading"]

            await _dispatch_vehicle_command(
                vehicle_id,
                follow_command,
                source="rtb_follow",
                emit_ack=False,
                write_log=False,
            )

            await asyncio.sleep(period_s)
    except asyncio.CancelledError:
        return
    finally:
        current_task = _rtb_follow_tasks.get(vehicle_id)
        if current_task is asyncio.current_task():
            _rtb_follow_tasks.pop(vehicle_id, None)

async def _stop_land_on_boat(vehicle_id: str) -> None:
    """Cancel and await a vehicle's running landing task, if any."""
    task = _land_on_boat_tasks.pop(vehicle_id, None)
    if not task:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[LAND] Stop task error for {vehicle_id}: {exc}")


async def _start_land_on_boat(vehicle_id: str, source: str) -> None:
    """Stop RTB or existing landing tasks and start the landing loop."""
    await _stop_land_on_boat(vehicle_id)
    await _stop_rtb_follow(vehicle_id)
    task = asyncio.create_task(_land_on_boat_loop(vehicle_id), name=f"land-boat-{vehicle_id}")
    _land_on_boat_tasks[vehicle_id] = task
    print(f"[LAND] Started land-on-boat sequence for {vehicle_id} (source={source})")


async def _land_on_boat_loop(vehicle_id: str) -> None:
    """Track the moving pad, descend to a hover clearance, and hold there."""
    STATE_APPROACH = 0
    STATE_DESCENT = 1
    STATE_HOVER = 2

    current_state = STATE_APPROACH
    target_alt_m: Optional[float] = None

    try:
        while True:
            period_s = 0.05  # High-rate 20 Hz loop for smooth dynamic tracking
            step_command: Optional[dict[str, Any]] = None

            async with state_lock:
                target_vehicle = vehicles.get(vehicle_id)
                yp_vehicle = _select_yp_vehicle_locked()

                if not target_vehicle or not target_vehicle.get("connected") or not yp_vehicle:
                    print(f"[LAND] Vehicle or YP unavailable; cancelling land sequence for {vehicle_id}")
                    return

                target_pos = target_vehicle.get("position") or {}
                yp_pos = yp_vehicle.get("position") or {}

                if yp_pos.get("latitude") is None or target_pos.get("latitude") is None:
                    target_alt_m = None
                else:
                    yp_heading = float(yp_vehicle.get("heading") or 0.0) % 360.0

                    yp_history = yp_vehicle.get("history") or []
                    yp_speed_mps = 0.0
                    if len(yp_history) >= 2:
                        p1, p2 = yp_history[-2], yp_history[-1]
                        dt = float(p2.get("stamp") or 0) - float(p1.get("stamp") or 0)
                        if dt > 0:
                            yp_speed_mps = _haversine_m(
                                p1["latitude"], p1["longitude"],
                                p2["latitude"], p2["longitude"],
                            ) / dt

                    pad_lat, pad_lon = _destination_point(
                        float(yp_pos["latitude"]),
                        float(yp_pos["longitude"]),
                        yp_heading + 180.0,
                        LAND_ON_BOAT_PAD_OFFSET_M,
                    )
                    horiz_dist_m = _haversine_m(
                        float(target_pos["latitude"]), float(target_pos["longitude"]),
                        pad_lat, pad_lon,
                    )
                    pad_hover_alt_m = float(yp_pos.get("altitude") or 0.0) + LAND_ON_BOAT_HOVER_CLEARANCE_M
                    vehicle_alt_m = float(target_pos.get("altitude") or 0.0)

                    # Avoid an abrupt first altitude command; descend from the
                    # current reported altitude toward the pad-relative target.
                    if target_alt_m is None:
                        target_alt_m = max(vehicle_alt_m, pad_hover_alt_m)

                    sink_rate_ms = 0.0
                    if current_state == STATE_APPROACH and horiz_dist_m <= LAND_ON_BOAT_ALIGNMENT_RADIUS_M:
                        current_state = STATE_DESCENT
                        print(f"[LAND] Aligned with pad; descending to {LAND_ON_BOAT_HOVER_CLEARANCE_M:.2f}m hover on {vehicle_id}")

                    if current_state == STATE_DESCENT:
                        target_alt_m = max(
                            pad_hover_alt_m,
                            target_alt_m - LAND_ON_BOAT_DESCENT_RATE_MS * period_s,
                        )
                        if target_alt_m <= pad_hover_alt_m:
                            target_alt_m = pad_hover_alt_m
                            current_state = STATE_HOVER
                            print(f"[LAND] Hover clearance reached on {vehicle_id}; holding above pad")
                        else:
                            sink_rate_ms = LAND_ON_BOAT_DESCENT_RATE_MS

                    if current_state == STATE_HOVER:
                        target_alt_m = pad_hover_alt_m

                    step_command = {
                        "type": "land_on_boat_step",
                        "target": {
                            "latitude": pad_lat,
                            "longitude": pad_lon,
                            "altitude": target_alt_m,
                        },
                        "heading": yp_heading,
                        "velocity_north_ms": yp_speed_mps * math.cos(math.radians(yp_heading)),
                        "velocity_east_ms": yp_speed_mps * math.sin(math.radians(yp_heading)),
                        "sink_rate_ms": sink_rate_ms,
                    }

            if step_command is not None:
                await _dispatch_vehicle_command(vehicle_id, step_command, source="land_on_boat", emit_ack=False, write_log=False)
            await asyncio.sleep(period_s)

    except asyncio.CancelledError:
        return
    finally:
        current_task = _land_on_boat_tasks.get(vehicle_id)
        if current_task is asyncio.current_task():
            _land_on_boat_tasks.pop(vehicle_id, None)

async def _deconfliction_check_loop() -> None:
    """Periodically check for vehicle conflicts and issue deconfliction commands."""
    while True:
        try:
            await asyncio.sleep(0.5)  # Check every 0.5 seconds
            await _check_vehicle_conflicts()
        except asyncio.CancelledError:
            return
        except Exception as error:
            print(f"[DECONFLICTION] Check failed: {error}")


async def _check_vehicle_conflicts() -> None:
    """Check for vehicle conflicts and issue deconfliction commands."""
    if not deconfliction_engine.enabled:
        return

    async with _deconfliction_lock:
        conflicts = deconfliction_engine.detect_conflicts()

        conflicted_vehicle_ids = {low_priority_id for low_priority_id, _ in conflicts}
        resume_commands = [
            (vehicle_id, deconfliction_engine.end_avoidance(vehicle_id))
            for vehicle_id in deconfliction_engine.active_avoidance_vehicle_ids() - conflicted_vehicle_ids
        ]
        avoidance_commands = []
        for low_priority_id, high_priority_id in conflicts:
            state_low = deconfliction_engine.vehicle_states.get(low_priority_id)
            if not state_low or state_low.is_paused or not deconfliction_engine.begin_avoidance(low_priority_id):
                continue
            waypoint = deconfliction_engine.calculate_deconfliction_waypoint(low_priority_id, high_priority_id)
            if waypoint:
                avoidance_commands.append((low_priority_id, high_priority_id, waypoint))
            else:
                deconfliction_engine.end_avoidance(low_priority_id)

    for vehicle_id, saved_command in resume_commands:
        if saved_command:
            await route_command(vehicle_id, saved_command, source="deconfliction_resume")
            print(f"[DECONFLICTION] Resumed mission for {vehicle_id}")

    for low_priority_id, high_priority_id, waypoint in avoidance_commands:
        orbit_command = {
            "type": "waypoint",
            "target": {
                "latitude": waypoint[0],
                "longitude": waypoint[1],
                "altitude": waypoint[2],
            },
            "_deconfliction": True,
            "_conflict_with": high_priority_id,
        }
        await route_command(low_priority_id, orbit_command, source="deconfliction")
        print(f"[DECONFLICTION] Issued avoidance waypoint to {low_priority_id} for {high_priority_id}")


async def _update_deconfliction_state(vehicle_id: str, vehicle: dict[str, Any], command: dict[str, Any]) -> None:
    """Update deconfliction engine with vehicle state after a command."""
    cmd_type = command.get("type")
    if cmd_type not in MISSION_PRIORITY:
        return
    
    position = vehicle.get("position")
    if not position or position.get("latitude") is None:
        return
    
    async with _deconfliction_lock:
        deconfliction_engine.update_vehicle(
            vehicle_id,
            vehicle.get("vehicle_type", "uav"),
            position,
            cmd_type,
        )
        # Save command for resume
        deconfliction_engine.set_saved_command(vehicle_id, command)


async def broadcast_ui(payload: dict[str, Any]) -> None:
    """Send a JSON payload to every connected UI WebSocket, dropping stale ones."""
    stale: list[WebSocket] = []
    for websocket in list(ui_connections):
        try:
            await websocket.send_json(payload)
        except Exception:
            stale.append(websocket)
    for websocket in stale:
        ui_connections.discard(websocket)


async def broadcast_ros(topic: str, msg: dict[str, Any], msg_type: str) -> None:
    """Publish a message to rosbridge clients subscribed to its topic (or \"*\")."""
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


def _do_influx_write(point: Any) -> None:
    """Blocking InfluxDB write — always called from a daemon thread."""
    try:
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
    except Exception as exc:
        print(f"Influx write failed: {exc}")


def write_influx(payload: dict[str, Any]) -> None:
    """Build an InfluxDB point from a message payload and enqueue it for writing."""
    if not write_api:
        return
    if _is_heartbeat_payload(payload):
        return
    if not should_write_influx(payload):
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
    except Exception as exc:
        print(f"Influx point build failed: {exc}")
        return
    # Drop the point onto the persistent writer queue; never stalls the event loop.
    try:
        _influx_write_queue.put_nowait(point)
    except _stdlib_queue.Full:
        pass  # drop under back-pressure rather than stall


def _is_heartbeat_payload(payload: dict[str, Any]) -> bool:
    """Return whether an incoming payload is a heartbeat message."""
    message_type = str(payload.get("type", ""))
    topic = str(payload.get("topic", ""))
    return message_type.lower().endswith("heartbeat") or topic.lower().rstrip("/").endswith("/heartbeat")


def should_write_influx(payload: dict[str, Any]) -> bool:
    """Return whether this (vehicle, message type) pair is due for another Influx write."""
    max_hz = float(settings.get("influx_max_write_hz") or 0)
    if max_hz <= 0:
        return True
    vehicle_id = str(payload.get("vehicle_id", "unknown"))
    msg_type = str(payload.get("type", "unknown"))
    key = (vehicle_id, msg_type)
    now = time.time()
    last_write = last_influx_write_at.get(key)
    if last_write is not None and now - last_write < 1.0 / max_hz:
        return False
    last_influx_write_at[key] = now
    return True


async def influx_retention_loop() -> None:
    """Periodically purge InfluxDB messages older than the retention window."""
    while True:
        await asyncio.sleep(float(settings["message_cleanup_interval_seconds"]))
        await delete_expired_influx_messages()


async def delete_expired_influx_messages() -> None:
    """Delete yp_messages points older than the configured retention period."""
    if not delete_api:
        return
    retention_seconds = float(settings["message_retention_seconds"])
    stop = time.time() - retention_seconds
    if stop <= 0:
        return
    try:
        stop_time = datetime.fromtimestamp(stop, tz=timezone.utc)
        await asyncio.to_thread(
            delete_api.delete,
            start="1970-01-01T00:00:00Z",
            stop=stop_time,
            predicate='_measurement="yp_messages"',
            bucket=INFLUX_BUCKET,
            org=INFLUX_ORG,
        )
    except Exception as exc:
        print(f"Influx retention cleanup failed: {exc}")


def add_fields(point: Point, value: Any, prefix: str = "") -> None:
    """Recursively flatten a nested message value into InfluxDB point fields."""
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
    """Convert a rosbridge publish payload into the internal vehicle-message shape."""
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
    """Return the client-facing snapshot of a vehicle, stripping private keys."""
    # Exclude private/internal keys (prefixed with "_") from the public representation.
    snapshot = {k: v for k, v in vehicle.items() if not k.startswith("_")}
    if isinstance(snapshot.get("history"), deque):
        snapshot["history"] = list(snapshot["history"])
    
    # Allow the vehicle's dynamic video payload to override the static server config
    if "video" not in snapshot:
        entry = video_streams.get(str(snapshot.get("vehicle_id") or ""))
        if entry:
            snapshot["video"] = public_video_stream(entry)
            
    return snapshot


def topic_vehicle_id(topic: str) -> Optional[str]:
    """Extract the vehicle id from a \"/vehicles/{id}/...\" topic string."""
    parts = [part for part in topic.split("/") if part]
    if len(parts) >= 2 and parts[0] == "vehicles":
        return parts[1]
    return None


def infer_vehicle_type(vehicle_id: str) -> str:
    """Guess a vehicle's type from a keyword substring in its id, defaulting to \"uav\"."""
    lower = vehicle_id.lower()
    for candidate in ("uav", "usv", "ugv", "uuv", "yp"):
        if candidate in lower:
            return candidate
    return "uav"


def normalize_vehicle_type(value: Any) -> str:
    """Coerce a value to a known vehicle type string, defaulting to \"uav\"."""
    text = str(value or "uav").lower()
    return text if text in {"uav", "usv", "ugv", "uuv", "yp"} else "uav"


def extract_navsatfix(topic: str, msg_type: str, msg: Any) -> Optional[dict[str, float]]:
    """Extract lat/lon/alt from a NavSatFix-shaped message, or None if not applicable."""
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
    """Extract position/orientation/yaw from a Pose-shaped message, or None if not applicable."""
    if not isinstance(msg, dict):
        return None
    if "Pose" not in msg_type and not topic.endswith("pose"):
        return None
    orientation = msg.get("orientation", {})
    yaw_deg = quaternion_to_yaw_deg(orientation) if isinstance(orientation, dict) else None
    return {"position": msg.get("position", {}), "orientation": orientation, "yaw_deg": yaw_deg}


def extract_battery(topic: str, msg_type: str, msg: Any) -> Optional[dict[str, Any]]:
    """Extract percentage/voltage/current from a BatteryState-shaped message, or None if not applicable."""
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
    """Extract a normalized 0-360 degree heading from a message, or None if absent."""
    if not isinstance(msg, dict):
        return None
    if "heading" in msg:
        return float(msg["heading"]) % 360
    return None


def quaternion_to_yaw_deg(q: dict[str, Any]) -> Optional[float]:
    """Convert a quaternion dict to a normalized 0-360 degree yaw angle."""
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



async def _set_rtcm_status(**updates: Any) -> None:
    """Merge fields into the global rtcm_status and push the new snapshot to connected UIs."""
    rtcm_status.update(updates)
    await broadcast_ui({"op": "rtcm_status_update", "status": dict(rtcm_status)})


# # Distribute RTCM correction frames for RTK fix distribution to all connected vehicles that can accept it
async def rtcm_ingest_loop():
    """Background loop that dynamically connects to configured RTCM sources with automatic retry logic."""
    global _rtcm_seq_id, _rtcm_last_broadcast_at
    loop = asyncio.get_running_loop()
    
    while True:
        # Fetch current settings from application runtime settings
        source_type = settings.get("rtk_source_type", "disabled") # "serial", "tcp", "udp", or "disabled"
        port_or_host = settings.get("rtk_host_or_port", "/dev/ttyACM0")
        network_port = int(settings.get("rtk_network_port", 9000))
        baudrate = int(settings.get("rtk_baudrate", 115200))

        if source_type == "disabled":
            if rtcm_status["state"] != "disabled":
                await _set_rtcm_status(state="disabled", source_type="disabled", target=None, error=None)
            await asyncio.sleep(2.0)
            continue

        target_label = port_or_host if source_type == "serial" else f"{port_or_host}:{network_port}"
        await _set_rtcm_status(state="connecting", source_type=source_type, target=target_label, error=None)

        buffer = bytearray()
        stream_reader = None

        try:
            if source_type == "serial":
                # Open USB/Serial base station
                stream_reader = await loop.run_in_executor(
                    None, lambda: serial.Serial(port_or_host, baudrate, timeout=1.0)
                )
                print(f"[RTCM] Connected to Serial base station at {port_or_host}")

            elif source_type == "tcp":
                # Connect to TCP Caster / Socket (e.g. Trimble Base over Ethernet)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(port_or_host, network_port), timeout=5.0
                )
                print(f"[RTCM] Connected to TCP base station at {port_or_host}:{network_port}")

            elif source_type == "udp":
                # Set up UDP socket reader
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind((port_or_host, network_port))
                sock.setblocking(False)
                print(f"[RTCM] Listening for UDP RTCM stream on {port_or_host}:{network_port}")

            await _set_rtcm_status(state="connected", error=None)

            # Stream processing loop
            while settings.get("rtk_source_type") == source_type:
                data = b""
                
                if source_type == "serial":
                    data = await loop.run_in_executor(None, lambda: stream_reader.read(1024))
                elif source_type == "tcp":
                    try:
                        data = await asyncio.wait_for(reader.read(1024), timeout=5.0)
                    except asyncio.TimeoutError:
                        data = b""
                elif source_type == "udp":
                    try:
                        data, _ = sock.recvfrom(2048)
                    except BlockingIOError:
                        await asyncio.sleep(0.02)
                        continue

                if not data:
                    await asyncio.sleep(0.01)
                    continue

                buffer.extend(data)

                # Parse RTCM3 frames starting with magic byte 0xD3
                while len(buffer) >= 3:
                    if buffer[0] != 0xD3:
                        buffer.pop(0)
                        continue

                    msg_len = ((buffer[1] & 0x03) << 8) | buffer[2]
                    frame_len = msg_len + 6  # 3 bytes header + payload + 3 bytes CRC

                    if len(buffer) < frame_len:
                        break

                    rtcm_frame = bytes(buffer[:frame_len])
                    buffer = buffer[frame_len:]

                    await distribute_rtcm_frame(rtcm_frame, _rtcm_seq_id)
                    _rtcm_seq_id = (_rtcm_seq_id + 1) % 32

                    now = time.time()
                    rtcm_status["frame_count"] += 1
                    rtcm_status["bytes_total"] += len(rtcm_frame)
                    rtcm_status["last_frame_at"] = now
                    # Throttle UI broadcasts so a fast correction stream doesn't flood the websocket
                    if now - _rtcm_last_broadcast_at > 0.5:
                        _rtcm_last_broadcast_at = now
                        await _set_rtcm_status(state="connected")

        except (serial.SerialException, asyncio.TimeoutError, OSError, ConnectionRefusedError) as exc:
            # Gracefully log stream outage without crashing the server
            print(f"[RTCM] Stream unavailable ({source_type}://{port_or_host}): {exc}. Retrying in 5 seconds...")
            await _set_rtcm_status(state="error", error=str(exc))
            await asyncio.sleep(5.0)  # Wait before attempting auto-reconnect
            
        finally:
            # Cleanup closed handles
            if source_type == "serial" and stream_reader and stream_reader.is_open:
                stream_reader.close()
            elif source_type == "tcp" and 'writer' in locals():
                writer.close()
            elif source_type == "udp" and 'sock' in locals():
                sock.close()


async def rtcm_watchdog_loop() -> None:
    """Mark the RTCM stream as \"stale\" if the transport is open but no frames have arrived recently."""
    STALE_AFTER_S = 8.0
    while True:
        await asyncio.sleep(2.0)
        if rtcm_status["state"] not in ("connected", "stale"):
            continue
        last_frame_at = rtcm_status["last_frame_at"]
        is_stale = last_frame_at is None or (time.time() - last_frame_at) > STALE_AFTER_S
        if is_stale and rtcm_status["state"] != "stale":
            await _set_rtcm_status(state="stale")
        elif not is_stale and rtcm_status["state"] != "connected":
            await _set_rtcm_status(state="connected")

def fragment_rtcm_frame(rtcm_bytes: bytes, sequence_id: int) -> list[dict]:
    """Break raw RTCM bytes into standard MAVLink GPS_RTCM_DATA payload dictionaries."""
    CHUNK_SIZE = 180
    chunks = [rtcm_bytes[i:i + CHUNK_SIZE] for i in range(0, len(rtcm_bytes), CHUNK_SIZE)]
    payloads = []
    
    for i, chunk in enumerate(chunks):
        is_fragmented = 1 if len(chunks) > 1 else 0
        fragment_id = i & 0x03
        
        # Flags bitmask: Bit 0 = IsFragmented | Bits 1-2 = Fragment ID | Bits 3-7 = Sequence ID
        flags = is_fragmented | (fragment_id << 1) | ((sequence_id & 0x1F) << 3)
        
        payloads.append({
            "flags": flags,
            "len": len(chunk),
            "data": list(chunk)  # Array of uint8_t bytes
        })
        
    return payloads


async def distribute_rtcm_frame(rtcm_frame: bytes, sequence_id: int):
    fragments = fragment_rtcm_frame(rtcm_frame, sequence_id)
    
    # Distribute fragments to all active vehicle queues
    for vehicle_id, queue in list(vehicle_queues.items()):
        for frag in fragments:
            cmd_payload = {
                "op": "command",
                "vehicle_id": vehicle_id,
                "source": "rtcm_service",
                "stamp": time.time(),
                "command": {
                    "type": "rtcm_data",
                    "flags": frag["flags"],
                    "len": frag["len"],
                    "data": frag["data"]
                }
            }
            try:
                queue.put_nowait(cmd_payload)
            except asyncio.QueueFull:
                pass  # Drop fragment under extreme network backpressure
