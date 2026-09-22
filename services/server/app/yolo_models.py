"""YOLO model registry: upload, list, and select the active detector model.

Uploaded weights are stored on a volume shared with the yolo-detector
container; selecting a model notifies the detector over its persistent
/ws/detector connection so it can hot-swap without a restart.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Body, Header, UploadFile
from fastapi.responses import JSONResponse

from app.auth import require_permission
from app.settings import get_detector_settings, update_detector_settings

MODELS_DIR = Path(os.getenv("YOLO_MODELS_DIR", "/data/yolo_models"))
ALLOWED_MODEL_EXTENSIONS = {".pt", ".onnx"}
MAX_MODEL_UPLOAD_BYTES = int(os.getenv("YOLO_MODEL_MAX_UPLOAD_MB", "512")) * 1024 * 1024

_active_model = os.getenv("YOLO_MODEL_PATH", "yolov8n.pt")
_conf_threshold = float(os.getenv("YOLO_CONF_THRESHOLD", "0.4"))
_infer_interval_seconds = float(os.getenv("YOLO_INFER_INTERVAL_SECONDS", "0.5"))
_broadcast: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None
_send_to_detector: Optional[Callable[[dict[str, Any]], Awaitable[bool]]] = None

router = APIRouter()


def set_broadcast_callback(fn: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
    """Register the coroutine used to fan out model registry updates to UI clients."""
    global _broadcast
    _broadcast = fn


def set_detector_sender(fn: Callable[[dict[str, Any]], Awaitable[bool]]) -> None:
    """Register the coroutine used to push commands to the connected detector service."""
    global _send_to_detector
    _send_to_detector = fn


def get_active_model() -> str:
    return _active_model


def get_settings() -> dict[str, float]:
    return {"conf_threshold": _conf_threshold, "infer_interval_seconds": _infer_interval_seconds}


def load_persisted_settings() -> None:
    """Restore conf_threshold/infer_interval_seconds saved from a previous run, if any."""
    global _conf_threshold, _infer_interval_seconds
    stored = get_detector_settings()
    if "conf_threshold" in stored:
        _conf_threshold = float(stored["conf_threshold"])
    if "infer_interval_seconds" in stored:
        _infer_interval_seconds = float(stored["infer_interval_seconds"])


def _safe_model_name(name: str) -> Optional[str]:
    """Reject path traversal / unsupported extensions; return the bare filename or None."""
    candidate = Path(name).name
    if not candidate or candidate != name.strip() or Path(candidate).suffix.lower() not in ALLOWED_MODEL_EXTENSIONS:
        return None
    return candidate


def _list_uploaded_models() -> list[str]:
    if not MODELS_DIR.is_dir():
        return []
    return sorted(p.name for p in MODELS_DIR.iterdir() if p.suffix.lower() in ALLOWED_MODEL_EXTENSIONS)


def _state() -> dict[str, Any]:
    # The bundled default (e.g. yolov8n.pt) isn't in MODELS_DIR but is always selectable.
    models = _list_uploaded_models()
    if _active_model not in models:
        models = [_active_model, *models]
    return {"models": models, "active_model": _active_model, **get_settings()}


@router.get("/api/detector/models")
async def list_models() -> dict[str, Any]:
    return _state()


@router.post("/api/detector/models")
async def upload_model(file: UploadFile, authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    authorization_error = require_permission(authorization, "control_cameras")
    if authorization_error:
        return authorization_error

    safe_name = _safe_model_name(file.filename or "")
    if not safe_name:
        return JSONResponse({"error": "invalid model filename (expected a .pt or .onnx file)"}, status_code=400)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest = MODELS_DIR / safe_name
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_MODEL_UPLOAD_BYTES:
                    raise ValueError(f"model exceeds {MAX_MODEL_UPLOAD_BYTES // (1024 * 1024)}MB limit")
                out.write(chunk)
    except ValueError as exc:
        dest.unlink(missing_ok=True)
        return JSONResponse({"error": str(exc)}, status_code=413)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        return JSONResponse({"error": f"upload failed: {exc}"}, status_code=500)

    if _broadcast:
        await _broadcast({"op": "detector_models_update", **_state()})
    return JSONResponse({"model": safe_name, **_state()})


@router.post("/api/detector/model")
async def select_model(payload: dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    authorization_error = require_permission(authorization, "control_cameras")
    if authorization_error:
        return authorization_error

    global _active_model
    name = str(payload.get("model", "")).strip()
    if not name or name not in _state()["models"]:
        return JSONResponse({"error": "unknown model"}, status_code=400)

    _active_model = name
    detector_connected = await _send_to_detector({"op": "set_model", "model": name}) if _send_to_detector else False
    if _broadcast:
        await _broadcast({"op": "detector_models_update", **_state()})
    return JSONResponse({**_state(), "detector_connected": detector_connected})


@router.post("/api/detector/settings")
async def update_settings(payload: dict[str, Any] = Body(default={}), authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    authorization_error = require_permission(authorization, "control_cameras")
    if authorization_error:
        return authorization_error

    global _conf_threshold, _infer_interval_seconds
    updates: dict[str, float] = {}
    try:
        if "conf_threshold" in payload:
            value = float(payload["conf_threshold"])
            if not 0.0 <= value <= 1.0:
                return JSONResponse({"error": "conf_threshold must be between 0 and 1"}, status_code=400)
            updates["conf_threshold"] = value
        if "infer_interval_seconds" in payload:
            value = float(payload["infer_interval_seconds"])
            if not 0.0 <= value <= 10.0:
                return JSONResponse({"error": "infer_interval_seconds must be between 0 and 10"}, status_code=400)
            updates["infer_interval_seconds"] = value
    except (TypeError, ValueError):
        return JSONResponse({"error": "settings values must be numbers"}, status_code=400)

    if not updates:
        return JSONResponse({"error": "no settings provided"}, status_code=400)

    if "conf_threshold" in updates:
        _conf_threshold = updates["conf_threshold"]
    if "infer_interval_seconds" in updates:
        _infer_interval_seconds = updates["infer_interval_seconds"]
    update_detector_settings(updates)

    detector_connected = await _send_to_detector({"op": "set_settings", **updates}) if _send_to_detector else False
    if _broadcast:
        await _broadcast({"op": "detector_models_update", **_state()})
    return JSONResponse({**_state(), "detector_connected": detector_connected})
