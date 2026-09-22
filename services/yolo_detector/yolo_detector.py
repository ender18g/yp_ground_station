"""YOLO object detection over Axis PTZ camera MJPEG feeds.

Pulls each online camera's proxied MJPEG stream from yp-server, runs YOLO
inference on it, and pushes bounding-box results back over /ws/detector so
the UI can overlay them on the live PTZ camera view.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

import cv2
import httpx
import torch
import websockets
from ultralytics import YOLO

SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://yp-server:8000/ws/detector")
SERVER_HTTP_URL = os.getenv("SERVER_HTTP_URL", "http://yp-server:8000")
MODELS_DIR = Path(os.getenv("YOLO_MODELS_DIR", "/data/yolo_models"))
# Directory the Dockerfile bakes the default model into, for offline first boot.
_BUNDLED_MODELS_DIR = Path(__file__).resolve().parent
CONF_THRESHOLD = float(os.getenv("YOLO_CONF_THRESHOLD", "0.4"))
INFER_INTERVAL_SECONDS = float(os.getenv("YOLO_INFER_INTERVAL_SECONDS", "0.5"))
# Inference resolution independent of the model's trained/native size - the single
# biggest lever for CPU inference cost, since larger custom models can otherwise
# run at 960/1280px and take seconds per frame instead of tens of milliseconds.
INFER_IMAGE_SIZE = int(os.getenv("YOLO_IMGSZ", "416"))
DISCOVERY_INTERVAL_SECONDS = float(os.getenv("YOLO_DISCOVERY_INTERVAL_SECONDS", "15"))
CAMERA_IDS_ENV = os.getenv("YOLO_CAMERA_IDS", "").strip()  # empty = auto-discover all online cameras
CLASS_FILTER = {c.strip() for c in os.getenv("YOLO_CLASSES", "").split(",") if c.strip()}
# "auto" uses the GPU when the container has one passed through (ROCm reports
# through the same torch.cuda API as CUDA); set to "cpu" to force CPU inference.
DEVICE = os.getenv("YOLO_DEVICE", "auto").strip().lower()

_send_queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
_camera_tasks: dict[str, asyncio.Task] = {}
_camera_tracks: dict[str, dict[int, dict[str, Any]]] = {}
_next_track_id: dict[str, int] = {}
_model_path = os.getenv("YOLO_MODEL_PATH", "yolov8n.pt")
_model: Optional[YOLO] = None


def _resolve_model_path(name: str) -> str:
    """Prefer an uploaded weights file, then the build-time bundled default,
    else fall back to a bare name that ultralytics can auto-download."""
    uploaded = MODELS_DIR / name
    if uploaded.is_file():
        return str(uploaded)
    bundled = _BUNDLED_MODELS_DIR / name
    if bundled.is_file():
        return str(bundled)
    return name


def _persist_model_file(path: str) -> None:
    """Copy a freshly loaded (bundled or auto-downloaded) model into the shared
    MODELS_DIR so it appears in the model list and survives container restarts."""
    source = Path(path)
    if not source.is_file():
        return
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest = MODELS_DIR / source.name
    if not dest.exists() and dest.resolve() != source.resolve():
        shutil.copy2(source, dest)


def _load_model() -> YOLO:
    global _model
    if _model is None:
        print(f"Loading YOLO model: {_model_path}")
        _model = YOLO(_model_path)
        device = DEVICE if DEVICE != "auto" else ("cuda:0" if torch.cuda.is_available() else "cpu")
        _model.to(device)
        print(f"YOLO inference device: {device}" + (f" ({torch.cuda.get_device_name(0)})" if device.startswith("cuda") else ""))
        _persist_model_file(_model_path)
    return _model


def _set_model(name: str) -> None:
    global _model_path, _model
    _model_path = _resolve_model_path(name)
    _model = None
    print(f"Active YOLO model set to: {name}")


def _apply_settings(payload: dict[str, Any]) -> None:
    """Live-update tunables pushed from the server's detector settings UI."""
    global CONF_THRESHOLD, INFER_INTERVAL_SECONDS
    if "conf_threshold" in payload:
        CONF_THRESHOLD = float(payload["conf_threshold"])
    if "infer_interval_seconds" in payload:
        INFER_INTERVAL_SECONDS = float(payload["infer_interval_seconds"])
    print(f"Detector settings updated: conf_threshold={CONF_THRESHOLD}, infer_interval_seconds={INFER_INTERVAL_SECONDS}")


def _box_iou(first: list[float], second: list[float]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union else 0.0


def _assign_track_ids(camera_id: str, detections: list[dict[str, Any]], timestamp: float) -> None:
    tracks = _camera_tracks.setdefault(camera_id, {})
    used_track_ids: set[int] = set()
    for detection in detections:
        best_id = None
        best_iou = 0.3
        for track_id, track in tracks.items():
            if track_id in used_track_ids or track["label"] != detection["label"]:
                continue
            overlap = _box_iou(track["box"], detection["box"])
            if overlap > best_iou:
                best_id = track_id
                best_iou = overlap
        if best_id is None:
            best_id = _next_track_id.get(camera_id, 0) + 1
            _next_track_id[camera_id] = best_id
        detection["track_id"] = best_id
        used_track_ids.add(best_id)
        tracks[best_id] = {"label": detection["label"], "box": detection["box"], "last_seen": timestamp}
    for track_id in list(tracks):
        if timestamp - tracks[track_id]["last_seen"] > 1.5:
            tracks.pop(track_id, None)


async def _fetch_online_camera_ids(client: httpx.AsyncClient) -> set[str]:
    if CAMERA_IDS_ENV:
        return {c.strip() for c in CAMERA_IDS_ENV.split(",") if c.strip()}
    try:
        response = await client.get(f"{SERVER_HTTP_URL}/api/cameras", timeout=5.0)
        response.raise_for_status()
        cameras = response.json().get("cameras", [])
        return {c["id"] for c in cameras if c.get("online")}
    except Exception as exc:
        print(f"Camera discovery failed: {exc}")
        return set()


async def _detect_camera(camera_id: str) -> None:
    """Continuously read frames for one camera and queue detection results."""
    stream_url = f"{SERVER_HTTP_URL}/api/cameras/{camera_id}/stream.mjpg"
    loop = asyncio.get_running_loop()
    print(f"[{camera_id}] starting detection loop from {stream_url}")

    while True:
        cap = await loop.run_in_executor(None, cv2.VideoCapture, stream_url)
        try:
            if not cap.isOpened():
                await asyncio.sleep(5.0)
                continue
            last_infer = 0.0
            while True:
                ok, frame = await loop.run_in_executor(None, cap.read)
                if not ok or frame is None:
                    break
                now = time.time()
                if now - last_infer < INFER_INTERVAL_SECONDS:
                    continue
                last_infer = now

                height, width = frame.shape[:2]
                model = _load_model()
                results = await loop.run_in_executor(
                    None, lambda: model(frame, conf=CONF_THRESHOLD, imgsz=INFER_IMAGE_SIZE, verbose=False)
                )
                detections = []
                for result in results:
                    names = result.names
                    for box in result.boxes:
                        label = names[int(box.cls[0])]
                        if CLASS_FILTER and label not in CLASS_FILTER:
                            continue
                        x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
                        detections.append({
                            "label": label,
                            "confidence": round(float(box.conf[0]), 3),
                            "box": [x1, y1, x2, y2],
                        })
                _assign_track_ids(camera_id, detections, now)
                await _send_queue.put({
                    "op": "camera_detection_update",
                    "camera_id": camera_id,
                    "frame_width": width,
                    "frame_height": height,
                    "detections": detections,
                    "timestamp": now,
                })
        except Exception as exc:
            print(f"[{camera_id}] detection loop error: {exc}")
        finally:
            await loop.run_in_executor(None, cap.release)
        await asyncio.sleep(5.0)


async def _discovery_loop() -> None:
    """Start/stop per-camera detection tasks as cameras come online/offline."""
    async with httpx.AsyncClient() as client:
        while True:
            online_ids = await _fetch_online_camera_ids(client)
            for camera_id in online_ids:
                if camera_id not in _camera_tasks or _camera_tasks[camera_id].done():
                    _camera_tasks[camera_id] = asyncio.create_task(_detect_camera(camera_id))
            for camera_id in list(_camera_tasks):
                if camera_id not in online_ids:
                    _camera_tasks.pop(camera_id).cancel()
            await asyncio.sleep(DISCOVERY_INTERVAL_SECONDS)


async def _sender_loop() -> None:
    """Maintain a persistent connection to the server, forwarding queued detections
    and applying any set_model commands the server pushes back."""
    while True:
        try:
            async with websockets.connect(SERVER_WS_URL, ping_interval=30, ping_timeout=20) as ws:
                print(f"YOLO detector connected to {SERVER_WS_URL}")
                drain_task = asyncio.create_task(_drain_send_queue(ws))
                try:
                    async for raw in ws:
                        try:
                            message = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if message.get("op") == "set_model" and message.get("model"):
                            _set_model(str(message["model"]))
                        elif message.get("op") == "set_settings":
                            _apply_settings(message)
                finally:
                    drain_task.cancel()
        except Exception as exc:
            print(f"Detector WS connection error: {exc}; retrying in 5s")
            await asyncio.sleep(5.0)


async def _drain_send_queue(ws) -> None:
    while True:
        message = await _send_queue.get()
        await ws.send(json.dumps(message))


async def main() -> None:
    _load_model()
    await asyncio.gather(_discovery_loop(), _sender_loop())


if __name__ == "__main__":
    asyncio.run(main())
