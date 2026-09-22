import { Camera, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Minus, Plus, Square, Upload, X } from "lucide-react";
import { useEffect, useRef, useState, type ChangeEvent, type PointerEvent as ReactPointerEvent, type WheelEvent } from "react";

import { getCameraTrack, getTrackSettings, listAxisCameras, listDetectorModels, selectDetectorModel, sendAxisPtz, setCameraTrack, updateDetectorSettings, updateTrackSettings, uploadDetectorModel, type AxisCamera, type CameraDetectionUpdate } from "../api";

type CameraPanelProps = {
  cameras: AxisCamera[];
  detections?: Record<string, CameraDetectionUpdate>;
  onClose: () => void;
};

type PtzVector = { pan: number; tilt: number; zoom: number };
type CameraPanelTab = "cameras" | "detection";

const PTZ_STOP: PtzVector = { pan: 0, tilt: 0, zoom: 0 };

// Detections older than this are considered stale (camera/detector lag) and hidden.
const DETECTION_MAX_AGE_SECONDS = 3;
// Delay slider-driven settings updates so dragging doesn't flood the server with requests.
const SETTINGS_DEBOUNCE_MS = 250;
// Fixed width:height ratio used only as a fallback when no video is available to measure (e.g. Detection tab).
const PANEL_ASPECT_RATIO = 420 / 520;
const PANEL_MIN_WIDTH = 320;
const PANEL_MIN_HEIGHT = PANEL_MIN_WIDTH / PANEL_ASPECT_RATIO;
// Assumed video shape until the stream's real dimensions are known (most Axis feeds are 16:9).
const DEFAULT_STREAM_ASPECT_RATIO = 16 / 9;

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
  const [tab, setTab] = useState<CameraPanelTab>("cameras");
  const [models, setModels] = useState<string[]>([]);
  const [activeModel, setActiveModel] = useState("");
  const [confThreshold, setConfThreshold] = useState(0.4);
  const [inferInterval, setInferInterval] = useState(0.5);
  const [uploading, setUploading] = useState(false);
  const [tracking, setTracking] = useState(false);
  const [trackGain, setTrackGain] = useState(160);
  const [trackMaxSpeed, setTrackMaxSpeed] = useState(60);
  const [trackDeadzone, setTrackDeadzone] = useState(0.04);
  const [streamAspect, setStreamAspect] = useState(DEFAULT_STREAM_ASPECT_RATIO);
  const dragRef = useRef<{ mode: "move" | "resize"; pointerId: number; startX: number; startY: number; frame: typeof frame; chromeHeight: number | null } | null>(null);
  const imageDragRef = useRef<{ pointerId: number; x: number; y: number } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const previewWrapRef = useRef<HTMLDivElement>(null);
  const settingsDebounceRef = useRef<number | null>(null);
  const hasAutoSizedRef = useRef(false);

  useEffect(() => setCameras(initialCameras), [initialCameras]);

  const selected = cameras.find((camera) => camera.id === selectedId) ?? cameras[0];
  const selectedDetections = selected ? detections?.[selected.id] : undefined;
  const isDetectionFresh = !!selectedDetections && Date.now() / 1000 - selectedDetections.timestamp < DETECTION_MAX_AGE_SECONDS;

  // Shrink the panel's initial height once to match the real video shape, so it doesn't
  // open with a portrait-shaped default frame around a landscape stream.
  useEffect(() => {
    if (hasAutoSizedRef.current || tab !== "cameras" || !selected) return;
    const previewHeight = previewWrapRef.current?.getBoundingClientRect().height;
    if (!previewHeight) return;
    hasAutoSizedRef.current = true;
    setFrame((prev) => {
      const chromeHeight = prev.height - previewHeight;
      const desiredHeight = chromeHeight + prev.width / streamAspect;
      return { ...prev, height: Math.min(desiredHeight, window.innerHeight - prev.y - 12) };
    });
  }, [tab, selected, streamAspect]);

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

  const refreshModels = () => {
    void listDetectorModels().then(({ models: next, active_model, conf_threshold, infer_interval_seconds }) => {
      setModels(next);
      setActiveModel(active_model);
      setConfThreshold(conf_threshold);
      setInferInterval(infer_interval_seconds);
    });
  };

  useEffect(() => {
    refreshModels();
    const timer = window.setInterval(refreshModels, 15000);
    return () => window.clearInterval(timer);
  }, []);

  const refreshTracking = () => {
    if (!selected) return;
    void getCameraTrack(selected.id).then(setTracking);
  };

  useEffect(() => {
    refreshTracking();
    const timer = window.setInterval(refreshTracking, 5000);
    return () => window.clearInterval(timer);
  }, [selected?.id]);

  const toggleTracking = (enabled: boolean) => {
    if (!selected) return;
    setError(null);
    setTracking(enabled);
    void setCameraTrack(selected.id, enabled)
      .then(setTracking)
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : "Track toggle failed");
        refreshTracking();
      });
  };

  useEffect(() => {
    void getTrackSettings().then(({ track_gain, track_max_speed, track_deadzone }) => {
      setTrackGain(track_gain);
      setTrackMaxSpeed(track_max_speed);
      setTrackDeadzone(track_deadzone);
    });
  }, []);

  const trackSettingsDebounceRef = useRef<number | null>(null);

  const applyTrackSettingsDebounced = (patch: Partial<{ track_gain: number; track_max_speed: number; track_deadzone: number }>) => {
    if (trackSettingsDebounceRef.current) window.clearTimeout(trackSettingsDebounceRef.current);
    trackSettingsDebounceRef.current = window.setTimeout(() => {
      void updateTrackSettings(patch).catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : "Track settings update failed");
      });
    }, SETTINGS_DEBOUNCE_MS);
  };

  const changeTrackGain = (value: number) => {
    setTrackGain(value);
    applyTrackSettingsDebounced({ track_gain: value });
  };

  const changeTrackMaxSpeed = (value: number) => {
    setTrackMaxSpeed(value);
    applyTrackSettingsDebounced({ track_max_speed: value });
  };

  const changeTrackDeadzone = (value: number) => {
    setTrackDeadzone(value);
    applyTrackSettingsDebounced({ track_deadzone: value });
  };

  const changeModel = (model: string) => {
    setError(null);
    void selectDetectorModel(model)
      .then(({ models: next, active_model }) => {
        setModels(next);
        setActiveModel(active_model);
      })
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Model selection failed"));
  };

  const uploadModel = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setError(null);
    setUploading(true);
    void uploadDetectorModel(file)
      .then(({ models: next, active_model }) => {
        setModels(next);
        setActiveModel(active_model);
      })
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Model upload failed"))
      .finally(() => setUploading(false));
  };

  const applySettingsDebounced = (settings: { conf_threshold?: number; infer_interval_seconds?: number }) => {
    if (settingsDebounceRef.current) window.clearTimeout(settingsDebounceRef.current);
    settingsDebounceRef.current = window.setTimeout(() => {
      void updateDetectorSettings(settings).catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : "Settings update failed");
      });
    }, SETTINGS_DEBOUNCE_MS);
  };

  const changeConfThreshold = (value: number) => {
    setConfThreshold(value);
    applySettingsDebounced({ conf_threshold: value });
  };

  const changeInferInterval = (value: number) => {
    setInferInterval(value);
    applySettingsDebounced({ infer_interval_seconds: value });
  };

  const move = (vector: PtzVector) => {
    if (!selected) return;
    setError(null);
    void sendAxisPtz(selected.id, vector.pan, vector.tilt, vector.zoom)
      .then(() => {
        // A manual move overrides auto-track server-side; reflect that in the toggle.
        if (vector.pan || vector.tilt || vector.zoom) refreshTracking();
      })
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : "PTZ command failed");
      });
  };

  const startWindowDrag = (mode: "move" | "resize", event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    // Non-video chrome (header/tabs/fields/PTZ controls) stays a fixed height; only the video area should grow.
    const previewHeight = previewWrapRef.current?.getBoundingClientRect().height ?? null;
    const chromeHeight = previewHeight !== null ? frame.height - previewHeight : null;
    dragRef.current = { mode, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, frame, chromeHeight };
  };

  const moveWindowDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;

    if (drag.mode === "move") {
      setFrame({
        width: drag.frame.width,
        height: drag.frame.height,
        x: Math.min(Math.max(12, drag.frame.x + dx), window.innerWidth - drag.frame.width - 12),
        y: Math.min(Math.max(12, drag.frame.y + dy), window.innerHeight - drag.frame.height - 12),
      });
      return;
    }

    if (drag.chromeHeight !== null) {
      // Video tab: scale the video area to the stream's real aspect ratio so resizing grows the
      // feed itself instead of the blank margin around it.
      let width = Math.max(PANEL_MIN_WIDTH, drag.frame.width + Math.max(dx, dy * streamAspect));
      width = Math.min(width, window.innerWidth - drag.frame.x - 12);
      let previewHeight = width / streamAspect;
      let height = drag.chromeHeight + previewHeight;
      const maxHeight = window.innerHeight - drag.frame.y - 12;
      if (height > maxHeight) {
        height = maxHeight;
        previewHeight = height - drag.chromeHeight;
        width = previewHeight * streamAspect;
      }
      const minHeight = drag.chromeHeight + PANEL_MIN_WIDTH / streamAspect;
      if (height < minHeight) {
        height = minHeight;
        width = PANEL_MIN_WIDTH;
      }
      setFrame({ width, height, x: drag.frame.x, y: drag.frame.y });
      return;
    }

    // Fallback (e.g. Detection tab, no video mounted yet): keep the panel's own aspect ratio.
    let width = Math.max(PANEL_MIN_WIDTH, drag.frame.width + Math.max(dx, dy * PANEL_ASPECT_RATIO));
    width = Math.min(width, window.innerWidth - drag.frame.x - 12);
    let height = width / PANEL_ASPECT_RATIO;
    const maxHeight = window.innerHeight - drag.frame.y - 12;
    if (height > maxHeight) {
      height = maxHeight;
      width = height * PANEL_ASPECT_RATIO;
    }
    if (height < PANEL_MIN_HEIGHT) {
      height = PANEL_MIN_HEIGHT;
      width = height * PANEL_ASPECT_RATIO;
    }
    setFrame({ width, height, x: drag.frame.x, y: drag.frame.y });
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
      <div className="camera-tabs" role="tablist" aria-label="Camera panel sections">
        <button
          role="tab"
          aria-selected={tab === "cameras"}
          className={tab === "cameras" ? "camera-tab active" : "camera-tab"}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={() => setTab("cameras")}
        >
          Cameras
        </button>
        <button
          role="tab"
          aria-selected={tab === "detection"}
          className={tab === "detection" ? "camera-tab active" : "camera-tab"}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={() => setTab("detection")}
        >
          Detection
        </button>
      </div>
      <div className="camera-panel-body">
        {tab === "cameras" && cameras.length === 0 && <p className="camera-panel-empty">No Axis cameras configured.</p>}
        {tab === "cameras" && cameras.length > 0 && selected && (
          <>
          <div className="camera-field">
            <label htmlFor="camera-select-input">Camera</label>
            <div className="camera-selector-row">
              <select id="camera-select-input" value={selected.id} onChange={(event) => setSelectedId(event.target.value)} onPointerDown={(event) => event.stopPropagation()}>
                {cameras.map((camera) => <option key={camera.id} value={camera.id}>{camera.label}{camera.online ? "" : " (offline)"}</option>)}
              </select>
              <span className={selected.online ? "camera-status online" : "camera-status"}>{selected.online ? "Online" : "Offline"}</span>
            </div>
          </div>
          <div className="camera-preview-wrap" ref={previewWrapRef}>
            {selected.online ? (
              <img
                className="camera-preview"
                src={selected.stream_url}
                alt={`${selected.label} live view`}
                onLoad={(event) => {
                  const img = event.currentTarget;
                  if (img.naturalWidth && img.naturalHeight) setStreamAspect(img.naturalWidth / img.naturalHeight);
                }}
                onPointerDown={startImageDrag}
                onPointerMove={moveImageDrag}
                onPointerUp={endImageDrag}
                onPointerCancel={endImageDrag}
                onWheel={zoomWithWheel}
              />
            ) : <div className="camera-offline"><Camera size={28} />Camera unavailable</div>}
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
        </>
      )}
      {tab === "detection" && (
        <>
          <div className="camera-field">
            <label htmlFor="detector-model-select">Detection Model</label>
            <p className="camera-field-hint">Active YOLO weights used for live object-detection overlays.</p>
            <div className="camera-model-row">
              <select id="detector-model-select" value={activeModel} onChange={(event) => changeModel(event.target.value)} onPointerDown={(event) => event.stopPropagation()}>
                {models.map((model) => <option key={model} value={model}>{model}</option>)}
              </select>
              <button
                className="icon-button"
                title="Upload a YOLO model (.pt/.onnx)"
                disabled={uploading}
                onPointerDown={(event) => event.stopPropagation()}
                onClick={() => fileInputRef.current?.click()}
              >
                <Upload size={15} />
              </button>
              <input ref={fileInputRef} type="file" accept=".pt,.onnx" className="camera-model-upload-input" onChange={uploadModel} />
            </div>
          </div>
          <div className="camera-field">
            <label htmlFor="detector-conf-threshold">Confidence Threshold: {confThreshold.toFixed(2)}</label>
            <p className="camera-field-hint">Minimum detection confidence required to draw a bounding box.</p>
            <input
              id="detector-conf-threshold"
              type="range"
              min={0.05}
              max={0.95}
              step={0.01}
              value={confThreshold}
              onChange={(event) => changeConfThreshold(parseFloat(event.target.value))}
              onPointerDown={(event) => event.stopPropagation()}
            />
          </div>
          <div className="camera-field">
            <label htmlFor="detector-infer-interval">Inference Interval: {inferInterval.toFixed(1)}s</label>
            <p className="camera-field-hint">Minimum time between inference passes per camera; lower runs more often but uses more CPU/GPU.</p>
            <input
              id="detector-infer-interval"
              type="range"
              min={0.1}
              max={3}
              step={0.1}
              value={inferInterval}
              onChange={(event) => changeInferInterval(parseFloat(event.target.value))}
              onPointerDown={(event) => event.stopPropagation()}
            />
          </div>
          <div className="camera-field">
            <label htmlFor="detector-ptz-track">PTZ Auto-Track</label>
            <p className="camera-field-hint">
              {selected
                ? `Automatically pan/tilt "${selected.label}" to keep the top detection centered.`
                : "Select a camera in the Cameras tab to enable tracking."}
            </p>
            <label className="camera-toggle">
              <input
                id="detector-ptz-track"
                type="checkbox"
                checked={tracking}
                disabled={!selected?.online || !selected?.ptz_capable}
                onChange={(event) => toggleTracking(event.target.checked)}
                onPointerDown={(event) => event.stopPropagation()}
              />
              <span>{tracking ? "Tracking on" : "Tracking off"}</span>
            </label>
          </div>
          <div className="camera-field">
            <label htmlFor="detector-track-gain">Track Gain: {trackGain.toFixed(0)}</label>
            <p className="camera-field-hint">How aggressively the camera steers toward an off-center detection; higher reacts faster but can overshoot.</p>
            <input
              id="detector-track-gain"
              type="range"
              min={20}
              max={400}
              step={5}
              value={trackGain}
              onChange={(event) => changeTrackGain(parseFloat(event.target.value))}
              onPointerDown={(event) => event.stopPropagation()}
            />
          </div>
          <div className="camera-field">
            <label htmlFor="detector-track-max-speed">Track Max Speed: {trackMaxSpeed.toFixed(0)}</label>
            <p className="camera-field-hint">Caps the pan/tilt speed used while auto-tracking.</p>
            <input
              id="detector-track-max-speed"
              type="range"
              min={5}
              max={100}
              step={1}
              value={trackMaxSpeed}
              onChange={(event) => changeTrackMaxSpeed(parseFloat(event.target.value))}
              onPointerDown={(event) => event.stopPropagation()}
            />
          </div>
          <div className="camera-field">
            <label htmlFor="detector-track-deadzone">Track Deadzone: {(trackDeadzone * 100).toFixed(0)}%</label>
            <p className="camera-field-hint">How close to center a detection must be before the camera stops nudging toward it (reduces jitter).</p>
            <input
              id="detector-track-deadzone"
              type="range"
              min={0}
              max={0.2}
              step={0.01}
              value={trackDeadzone}
              onChange={(event) => changeTrackDeadzone(parseFloat(event.target.value))}
              onPointerDown={(event) => event.stopPropagation()}
            />
          </div>
        </>
      )}
      </div>
      {error && <div className="camera-panel-error" role="alert">{error}</div>}
      <button className="camera-resize-handle" title="Resize camera window" onPointerDown={(event) => startWindowDrag("resize", event)} onPointerMove={moveWindowDrag} onPointerUp={endWindowDrag}><Plus size={13} /></button>
    </section>
  );
}