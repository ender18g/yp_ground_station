"""Axis VAPIX PTZ camera discovery, MJPEG proxying, and PTZ control.

Handles the port/starboard/aft cameras mounted on the YP. Each camera is
periodically probed over HTTP; when reachable it is published to UI clients
via the registered broadcast callback so the frontend can offer it as a
selectable stream, similarly to the existing MAVLink camera discovery flow.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import require_permission
from app.settings import get_detector_settings, update_detector_settings

AXIS_CAMERA_USERNAME = os.getenv("AXIS_CAMERA_USERNAME", "root")
AXIS_CAMERA_PASSWORD = os.getenv("AXIS_CAMERA_PASSWORD", "")
AXIS_CAMERA_PROBE_INTERVAL_SECONDS = float(os.getenv("AXIS_CAMERA_PROBE_INTERVAL_SECONDS", "15.0"))
AXIS_CAMERA_PROBE_TIMEOUT_SECONDS = float(os.getenv("AXIS_CAMERA_PROBE_TIMEOUT_SECONDS", "3.0"))
AXIS_CAMERAS_JSON = os.getenv("AXIS_CAMERAS_JSON", "")

# Proportional-control gains mapping a detection's offset from frame-center (as a fraction
# of frame size) to a PTZ continuous-move velocity (-100..100). Mutable/persistent - see
# get_track_settings()/set_track_settings() below.
_track_gain = float(os.getenv("AXIS_CAMERA_TRACK_GAIN", "160"))
_track_max_speed = float(os.getenv("AXIS_CAMERA_TRACK_MAX_SPEED", "60"))
_track_deadzone = float(os.getenv("AXIS_CAMERA_TRACK_DEADZONE", "0.04"))

# Known mounting positions on the YP. Hosts are optional (blank = not installed yet)
# and are configured individually so cameras can come online one at a time.
_DEFAULT_POSITIONS = [
    ("port", "Port"),
    ("starboard", "Starboard"),
    ("aft", "Aft"),
]

cameras: dict[str, dict[str, Any]] = {}
_probe_tasks: dict[str, asyncio.Task] = {}
_broadcast: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None
_http_client: Optional[httpx.AsyncClient] = None
_tracking_enabled: dict[str, bool] = {}

router = APIRouter()


def set_broadcast_callback(fn: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
    """Register the coroutine used to fan out camera status updates to UI clients."""
    global _broadcast
    _broadcast = fn


def _load_cameras() -> None:
    """Populate the camera registry from env vars: default port/starboard/aft
    slots (each with its own optional host/credential overrides), plus any
    additional cameras/overrides supplied via AXIS_CAMERAS_JSON."""
    for camera_id, label in _DEFAULT_POSITIONS:
        prefix = f"AXIS_CAMERA_{camera_id.upper()}"
        host = os.getenv(f"{prefix}_HOST", "").strip()
        cameras[camera_id] = {
            "id": camera_id,
            "label": label,
            "host": host,
            "username": os.getenv(f"{prefix}_USERNAME", AXIS_CAMERA_USERNAME),
            "password": os.getenv(f"{prefix}_PASSWORD", AXIS_CAMERA_PASSWORD),
            "online": False,
            "last_checked": None,
            "error": None,
        }

    if not AXIS_CAMERAS_JSON:
        return
    try:
        extra = json.loads(AXIS_CAMERAS_JSON)
    except json.JSONDecodeError as exc:
        print(f"AXIS_CAMERAS_JSON parse error: {exc}")
        return
    if not isinstance(extra, list):
        print("AXIS_CAMERAS_JSON must be a list of camera config objects")
        return
    for entry in extra:
        if not isinstance(entry, dict) or not str(entry.get("id", "")).strip():
            continue
        camera_id = str(entry["id"]).strip()
        existing = cameras.get(camera_id, {})
        cameras[camera_id] = {
            "id": camera_id,
            "label": str(entry.get("label", existing.get("label", camera_id))),
            "host": str(entry.get("host", existing.get("host", ""))).strip(),
            "username": str(entry.get("username", existing.get("username", AXIS_CAMERA_USERNAME))),
            "password": str(entry.get("password", existing.get("password", AXIS_CAMERA_PASSWORD))),
            "online": False,
            "last_checked": None,
            "error": None,
        }


def public_camera(entry: dict[str, Any]) -> dict[str, Any]:
    """Client-facing camera info (credentials never leave the server)."""
    return {
        "id": entry["id"],
        "label": entry["label"],
        "online": entry["online"],
        "last_checked": entry["last_checked"],
        "stream_url": f"/api/cameras/{entry['id']}/stream.mjpg",
        "ptz_capable": True,
    }


def _auth_for(entry: dict[str, Any]) -> Optional[httpx.DigestAuth]:
    if not entry.get("password"):
        return None
    return httpx.DigestAuth(entry["username"], entry["password"])


async def _probe_once(camera_id: str) -> None:
    entry = cameras.get(camera_id)
    if not entry or not entry["host"] or not _http_client:
        return
    was_online = entry["online"]
    url = f"http://{entry['host']}/axis-cgi/param.cgi?action=list&group=root.Brand.Brand"
    try:
        response = await _http_client.get(url, auth=_auth_for(entry), timeout=AXIS_CAMERA_PROBE_TIMEOUT_SECONDS)
        entry["online"] = response.status_code < 400
        entry["error"] = None if entry["online"] else f"HTTP {response.status_code}"
    except Exception as exc:
        entry["online"] = False
        entry["error"] = str(exc)
    entry["last_checked"] = time.time()
    if entry["online"] != was_online and _broadcast:
        await _broadcast({"op": "camera_status_update", "camera": public_camera(entry)})


async def _probe_loop(camera_id: str) -> None:
    while True:
        await _probe_once(camera_id)
        await asyncio.sleep(AXIS_CAMERA_PROBE_INTERVAL_SECONDS)


@router.on_event("startup")
async def startup() -> None:
    global _http_client
    _http_client = httpx.AsyncClient(timeout=AXIS_CAMERA_PROBE_TIMEOUT_SECONDS)
    _load_cameras()
    for camera_id, entry in cameras.items():
        if entry["host"]:
            _probe_tasks[camera_id] = asyncio.create_task(_probe_loop(camera_id))


@router.on_event("shutdown")
async def shutdown() -> None:
    for task in _probe_tasks.values():
        task.cancel()
    if _probe_tasks:
        await asyncio.gather(*_probe_tasks.values(), return_exceptions=True)
    if _http_client:
        await _http_client.aclose()


@router.get("/api/cameras")
async def list_cameras() -> dict[str, Any]:
    """List cameras that have a configured host (i.e. installed on the network)."""
    return {"cameras": [public_camera(entry) for entry in cameras.values() if entry["host"]]}


@router.get("/api/cameras/{camera_id}/stream.mjpg")
async def stream_camera(camera_id: str):
    """Proxy the camera's MJPEG stream so credentials stay server-side."""
    entry = cameras.get(camera_id)
    if not entry or not entry["host"]:
        return JSONResponse({"error": "camera not configured"}, status_code=404)

    url = f"http://{entry['host']}/axis-cgi/mjpg/video.cgi"
    client = httpx.AsyncClient(timeout=None)
    try:
        stream_ctx = client.stream("GET", url, auth=_auth_for(entry))
        upstream = await stream_ctx.__aenter__()
    except Exception as exc:
        await client.aclose()
        return JSONResponse({"error": f"camera unreachable: {exc}"}, status_code=502)

    if upstream.status_code >= 400:
        await stream_ctx.__aexit__(None, None, None)
        await client.aclose()
        return JSONResponse({"error": f"camera returned HTTP {upstream.status_code}"}, status_code=502)

    content_type = upstream.headers.get("content-type", "multipart/x-mixed-replace")

    async def proxy():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await stream_ctx.__aexit__(None, None, None)
            await client.aclose()

    return StreamingResponse(proxy(), media_type=content_type)


async def _send_ptz_move(camera_id: str, pan: float, tilt: float, zoom: float) -> tuple[bool, str]:
    entry = cameras.get(camera_id)
    if not entry or not entry["host"]:
        return False, "camera not configured"
    if not _http_client:
        return False, "camera client not ready"

    url = f"http://{entry['host']}/axis-cgi/com/ptz.cgi"
    params = {
        "camera": 1,
        "continuouspantiltmove": f"{pan:.0f},{tilt:.0f}",
        "continuouszoommove": f"{zoom:.0f}",
    }
    try:
        response = await _http_client.get(url, params=params, auth=_auth_for(entry), timeout=AXIS_CAMERA_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:
        return False, f"PTZ command failed: {exc}"
    if response.status_code >= 400:
        return False, f"camera returned HTTP {response.status_code}"
    return True, "ok"


def is_tracking(camera_id: str) -> bool:
    return _tracking_enabled.get(camera_id, False)


def get_track_settings() -> dict[str, float]:
    return {"track_gain": _track_gain, "track_max_speed": _track_max_speed, "track_deadzone": _track_deadzone}


def set_track_settings(patch: dict[str, float]) -> dict[str, float]:
    global _track_gain, _track_max_speed, _track_deadzone
    if "track_gain" in patch:
        _track_gain = float(patch["track_gain"])
    if "track_max_speed" in patch:
        _track_max_speed = float(patch["track_max_speed"])
    if "track_deadzone" in patch:
        _track_deadzone = float(patch["track_deadzone"])
    return get_track_settings()


def load_persisted_settings() -> None:
    """Restore track_gain/track_max_speed/track_deadzone saved from a previous run, if any."""
    stored = get_detector_settings()
    patch = {key: stored[key] for key in ("track_gain", "track_max_speed", "track_deadzone") if key in stored}
    if patch:
        set_track_settings(patch)


async def set_tracking(camera_id: str, enabled: bool) -> bool:
    """Enable/disable auto-track for a camera; broadcasts the new state to UI clients."""
    if camera_id not in cameras:
        return False
    _tracking_enabled[camera_id] = enabled
    if not enabled:
        await _send_ptz_move(camera_id, 0, 0, 0)
    if _broadcast:
        await _broadcast({"op": "camera_track_update", "camera_id": camera_id, "tracking": enabled})
    return True


async def track_from_detections(camera_id: str, detections: list[dict[str, Any]], frame_width: float, frame_height: float) -> None:
    """Steer the camera to keep the highest-confidence detection centered while tracking is enabled."""
    if not is_tracking(camera_id) or not frame_width or not frame_height:
        return
    if not detections:
        await _send_ptz_move(camera_id, 0, 0, 0)
        return

    best = max(detections, key=lambda d: d.get("confidence", 0))
    x1, y1, x2, y2 = best["box"]
    center_x = (x1 + x2) / 2
    center_y = (y1 + y2) / 2
    error_x = (center_x - frame_width / 2) / frame_width
    error_y = (frame_height / 2 - center_y) / frame_height
    if abs(error_x) < _track_deadzone:
        error_x = 0.0
    if abs(error_y) < _track_deadzone:
        error_y = 0.0
    pan = max(-_track_max_speed, min(_track_max_speed, error_x * _track_gain))
    tilt = max(-_track_max_speed, min(_track_max_speed, error_y * _track_gain))
    await _send_ptz_move(camera_id, pan, tilt, 0)



@router.post("/api/cameras/{camera_id}/ptz")
async def ptz_camera(camera_id: str, payload: dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    """Forward a continuous pan/tilt/zoom move (or stop, with all-zero values) to VAPIX."""
    authorization_error = require_permission(authorization, "control_cameras")
    if authorization_error:
        return authorization_error

    if camera_id not in cameras:
        return JSONResponse({"error": "camera not configured"}, status_code=404)

    try:
        pan = max(-100.0, min(100.0, float(payload.get("pan", 0))))
        tilt = max(-100.0, min(100.0, float(payload.get("tilt", 0))))
        zoom = max(-100.0, min(100.0, float(payload.get("zoom", 0))))
    except (TypeError, ValueError):
        return JSONResponse({"error": "pan, tilt, zoom must be numbers"}, status_code=400)

    # A manual move overrides auto-track so the two don't fight over the camera.
    if (pan or tilt or zoom) and is_tracking(camera_id):
        await set_tracking(camera_id, False)

    ok, message = await _send_ptz_move(camera_id, pan, tilt, zoom)
    if not ok:
        return JSONResponse({"error": message}, status_code=404 if message == "camera not configured" else 502)
    return JSONResponse({"ok": True})


@router.get("/api/cameras/{camera_id}/track")
async def get_track(camera_id: str) -> JSONResponse:
    if camera_id not in cameras:
        return JSONResponse({"error": "camera not configured"}, status_code=404)
    return JSONResponse({"camera_id": camera_id, "tracking": is_tracking(camera_id)})


@router.post("/api/cameras/{camera_id}/track")
async def post_track(camera_id: str, payload: dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    authorization_error = require_permission(authorization, "control_cameras")
    if authorization_error:
        return authorization_error

    enabled = bool(payload.get("enabled", False))
    if not await set_tracking(camera_id, enabled):
        return JSONResponse({"error": "camera not configured"}, status_code=404)
    return JSONResponse({"camera_id": camera_id, "tracking": enabled})


@router.get("/api/detector/track-settings")
async def get_track_settings_route() -> dict[str, float]:
    return get_track_settings()


@router.post("/api/detector/track-settings")
async def post_track_settings_route(payload: dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    authorization_error = require_permission(authorization, "control_cameras")
    if authorization_error:
        return authorization_error

    bounds = {"track_gain": (0.0, 1000.0), "track_max_speed": (0.0, 100.0), "track_deadzone": (0.0, 0.5)}
    patch: dict[str, float] = {}
    try:
        for key, (minimum, maximum) in bounds.items():
            if key in payload:
                value = float(payload[key])
                if not minimum <= value <= maximum:
                    return JSONResponse({"error": f"{key} must be between {minimum} and {maximum}"}, status_code=400)
                patch[key] = value
    except (TypeError, ValueError):
        return JSONResponse({"error": "settings values must be numbers"}, status_code=400)

    if not patch:
        return JSONResponse({"error": "no settings provided"}, status_code=400)

    updated = set_track_settings(patch)
    update_detector_settings(patch)
    if _broadcast:
        await _broadcast({"op": "detector_track_settings_update", **updated})
    return JSONResponse(updated)


