"""YOLO object detection over Axis PTZ camera MJPEG feeds.

Pulls each online camera's proxied MJPEG stream from yp-server, runs YOLO
inference on it, and pushes bounding-box results back over /ws/detector so
the UI can overlay them on the live PTZ camera view.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Optional

import cv2
import httpx
import websockets
from ultralytics import YOLO

SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://yp-server:8000/ws/detector")
SERVER_HTTP_URL = os.getenv("SERVER_HTTP_URL", "http://yp-server:8000")
MODEL_PATH = os.getenv("YOLO_MODEL_PATH", "yolov8n.pt")
CONF_THRESHOLD = float(os.getenv("YOLO_CONF_THRESHOLD", "0.4"))
INFER_INTERVAL_SECONDS = float(os.getenv("YOLO_INFER_INTERVAL_SECONDS", "0.5"))
DISCOVERY_INTERVAL_SECONDS = float(os.getenv("YOLO_DISCOVERY_INTERVAL_SECONDS", "15"))
CAMERA_IDS_ENV = os.getenv("YOLO_CAMERA_IDS", "").strip()  # empty = auto-discover all online cameras
CLASS_FILTER = {c.strip() for c in os.getenv("YOLO_CLASSES", "").split(",") if c.strip()}

_send_queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
_camera_tasks: dict[str, asyncio.Task] = {}
_model: Optional[YOLO] = None


def _load_model() -> YOLO:
    global _model
    if _model is None:
        print(f"Loading YOLO model: {MODEL_PATH}")
        _model = YOLO(MODEL_PATH)
    return _model


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
    model = _load_model()
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
                results = await loop.run_in_executor(
                    None, lambda: model(frame, conf=CONF_THRESHOLD, verbose=False)
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
    """Maintain a persistent connection to the server and forward queued detections."""
    while True:
        try:
            async with websockets.connect(SERVER_WS_URL, ping_interval=30, ping_timeout=20) as ws:
                print(f"YOLO detector connected to {SERVER_WS_URL}")
                while True:
                    message = await _send_queue.get()
                    await ws.send(json.dumps(message))
        except Exception as exc:
            print(f"Detector WS connection error: {exc}; retrying in 5s")
            await asyncio.sleep(5.0)


async def main() -> None:
    _load_model()
    await asyncio.gather(_discovery_loop(), _sender_loop())


if __name__ == "__main__":
    asyncio.run(main())
