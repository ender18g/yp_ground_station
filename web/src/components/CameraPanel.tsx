import { Camera, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Minus, Plus, Square, X } from "lucide-react";
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent } from "react";

import { listAxisCameras, sendAxisPtz, type AxisCamera, type CameraDetectionUpdate } from "../api";

type CameraPanelProps = {
  cameras: AxisCamera[];
  detections?: Record<string, CameraDetectionUpdate>;
  onClose: () => void;
};

type PtzVector = { pan: number; tilt: number; zoom: number };

const PTZ_STOP: PtzVector = { pan: 0, tilt: 0, zoom: 0 };

// Detections older than this are considered stale (camera/detector lag) and hidden.
const DETECTION_MAX_AGE_SECONDS = 3;

export function CameraPanel({ cameras: initialCameras, detections, onClose }: CameraPanelProps) {
  const [frame, setFrame] = useState(() => ({
    x: Math.max(12, window.innerWidth - 452),
    y: 64,
    width: Math.min(420, window.innerWidth - 24),
    height: 520,
  }));
  const [cameras, setCameras] = useState(initialCameras);
  const [selectedId, setSelectedId] = useState(initialCameras.find((camera) => camera.online)?.id ?? initialCameras[0]?.id ?? "");
  const [error, setError] = useState<string | null>(null);
  const dragRef = useRef<{ mode: "move" | "resize"; pointerId: number; startX: number; startY: number; frame: typeof frame } | null>(null);
  const imageDragRef = useRef<{ pointerId: number; x: number; y: number } | null>(null);

  useEffect(() => setCameras(initialCameras), [initialCameras]);

  const selected = cameras.find((camera) => camera.id === selectedId) ?? cameras[0];
  const selectedDetections = selected ? detections?.[selected.id] : undefined;
  const isDetectionFresh = !!selectedDetections && Date.now() / 1000 - selectedDetections.timestamp < DETECTION_MAX_AGE_SECONDS;

  const refresh = () => {
    void listAxisCameras().then((next) => {
      setCameras(next);
      setSelectedId((current) => next.some((camera) => camera.id === current) ? current : next[0]?.id ?? "");
    });
  };

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => window.clearInterval(timer);
  }, []);

  const move = (vector: PtzVector) => {
    if (!selected) return;
    setError(null);
    void sendAxisPtz(selected.id, vector.pan, vector.tilt, vector.zoom).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : "PTZ command failed");
    });
  };

  const startWindowDrag = (mode: "move" | "resize", event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { mode, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, frame };
  };

  const moveWindowDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    const width = drag.mode === "resize" ? Math.max(320, drag.frame.width + dx) : drag.frame.width;
    const height = drag.mode === "resize" ? Math.max(300, drag.frame.height + dy) : drag.frame.height;
    setFrame({
      width: Math.min(width, window.innerWidth - 24),
      height: Math.min(height, window.innerHeight - 24),
      x: drag.mode === "move" ? Math.min(Math.max(12, drag.frame.x + dx), window.innerWidth - width - 12) : drag.frame.x,
      y: drag.mode === "move" ? Math.min(Math.max(12, drag.frame.y + dy), window.innerHeight - height - 12) : drag.frame.y,
    });
  };

  const endWindowDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
  };

  const startImageDrag = (event: ReactPointerEvent<HTMLImageElement>) => {
    if (!selected?.online) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    imageDragRef.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY };
  };

  const moveImageDrag = (event: ReactPointerEvent<HTMLImageElement>) => {
    const drag = imageDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.x;
    const dy = drag.y - event.clientY;
    drag.x = event.clientX;
    drag.y = event.clientY;
    move({ pan: Math.max(-100, Math.min(100, dx * 3)), tilt: Math.max(-100, Math.min(100, dy * 3)), zoom: 0 });
  };

  const endImageDrag = (event: ReactPointerEvent<HTMLImageElement>) => {
    if (imageDragRef.current?.pointerId === event.pointerId) {
      imageDragRef.current = null;
      move(PTZ_STOP);
    }
  };

  const zoomWithWheel = (event: WheelEvent<HTMLImageElement>) => {
    event.preventDefault();
    move({ pan: 0, tilt: 0, zoom: event.deltaY < 0 ? 70 : -70 });
    window.setTimeout(() => move(PTZ_STOP), 120);
  };

  const hold = (vector: PtzVector) => ({
    onPointerDown: (event: React.PointerEvent<HTMLButtonElement>) => {
      event.currentTarget.setPointerCapture(event.pointerId);
      move(vector);
    },
    onPointerUp: () => move(PTZ_STOP),
    onPointerCancel: () => move(PTZ_STOP),
    onPointerLeave: () => move(PTZ_STOP),
  });

  return (
    <section className="camera-panel" aria-label="Axis cameras" style={{ left: frame.x, top: frame.y, width: frame.width, height: frame.height }}>
      <header className="camera-panel-header" onPointerDown={(event) => startWindowDrag("move", event)} onPointerMove={moveWindowDrag} onPointerUp={endWindowDrag}>
        <div className="panel-title"><Camera size={17} /><strong>YP Cameras</strong></div>
        <button className="icon-button" title="Close cameras" onPointerDown={(event) => event.stopPropagation()} onClick={onClose}><X size={17} /></button>
      </header>
      {cameras.length === 0 && <p className="camera-panel-empty">No Axis cameras configured.</p>}
      {cameras.length > 0 && selected && (
        <>
          <div className="camera-selector-row">
            <select value={selected.id} onChange={(event) => setSelectedId(event.target.value)} onPointerDown={(event) => event.stopPropagation()} aria-label="Camera">
              {cameras.map((camera) => <option key={camera.id} value={camera.id}>{camera.label}{camera.online ? "" : " (offline)"}</option>)}
            </select>
            <span className={selected.online ? "camera-status online" : "camera-status"}>{selected.online ? "Online" : "Offline"}</span>
          </div>
          <div className="camera-preview-wrap">
            {selected.online ? <img className="camera-preview" src={selected.stream_url} alt={`${selected.label} live view`} onPointerDown={startImageDrag} onPointerMove={moveImageDrag} onPointerUp={endImageDrag} onPointerCancel={endImageDrag} onWheel={zoomWithWheel} /> : <div className="camera-offline"><Camera size={28} />Camera unavailable</div>}
            {selected.online && isDetectionFresh && selectedDetections && (
              // preserveAspectRatio matches the img's object-fit: contain so boxes line up regardless of panel size.
              <svg
                className="camera-detection-overlay"
                viewBox={`0 0 ${selectedDetections.frame_width} ${selectedDetections.frame_height}`}
                preserveAspectRatio="xMidYMid meet"
              >
                {selectedDetections.detections.map((detection, index) => {
                  const [x1, y1, x2, y2] = detection.box;
                  return (
                    <g key={index}>
                      <rect x={x1} y={y1} width={x2 - x1} height={y2 - y1} className="camera-detection-box" />
                      <text x={x1} y={Math.max(0, y1 - 4)} className="camera-detection-label">
                        {detection.label} {Math.round(detection.confidence * 100)}%
                      </text>
                    </g>
                  );
                })}
              </svg>
            )}
          </div>
          {selected.online && selected.ptz_capable && (
            <div className="camera-ptz-controls" aria-label="PTZ controls">
              <button className="camera-ptz-up" title="Tilt up" {...hold({ pan: 0, tilt: 70, zoom: 0 })}><ChevronUp size={18} /></button>
              <button className="camera-ptz-left" title="Pan left" {...hold({ pan: -70, tilt: 0, zoom: 0 })}><ChevronLeft size={18} /></button>
              <button className="camera-ptz-stop" title="Stop movement" onClick={() => move(PTZ_STOP)}><Square size={13} /></button>
              <button className="camera-ptz-right" title="Pan right" {...hold({ pan: 70, tilt: 0, zoom: 0 })}><ChevronRight size={18} /></button>
              <button className="camera-ptz-down" title="Tilt down" {...hold({ pan: 0, tilt: -70, zoom: 0 })}><ChevronDown size={18} /></button>
              <button className="camera-ptz-zoom-out" title="Zoom out" {...hold({ pan: 0, tilt: 0, zoom: -70 })}><Minus size={16} /></button>
              <button className="camera-ptz-zoom-in" title="Zoom in" {...hold({ pan: 0, tilt: 0, zoom: 70 })}><Plus size={16} /></button>
            </div>
          )}
          {error && <div className="camera-panel-error" role="alert">{error}</div>}
        </>
      )}
      <button className="camera-resize-handle" title="Resize camera window" onPointerDown={(event) => startWindowDrag("resize", event)} onPointerMove={moveWindowDrag} onPointerUp={endWindowDrag}><Plus size={13} /></button>
    </section>
  );
}