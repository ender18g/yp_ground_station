import { Camera, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Minus, Plus, Square, Upload, X } from "lucide-react";
import { useEffect, useRef, useState, type ChangeEvent, type PointerEvent as ReactPointerEvent, type WheelEvent } from "react";

import { fetchSettings, getCameraTrack, getCameraTrackStates, getCoordinatedCameraTrack, getTrackSettings, listAxisCameras, listDetectorModels, selectDetectorModel, sendAxisPtz, setCameraTrack, setCoordinatedCameraTrack, updateCameraSpatial, updateDetectorSettings, updateSettings, updateTrackSettings, uploadDetectorModel, type AxisCamera, type CameraDetectionUpdate, type CameraSpatial } from "../api";

type CameraPanelProps = {
  cameras: AxisCamera[];
  detections?: Record<string, CameraDetectionUpdate>;
  onClose: () => void;
};

type PtzVector = { pan: number; tilt: number; zoom: number };
type CameraPanelTab = "cameras" | "all" | "spatial" | "detection";

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
  const [trackingByCamera, setTrackingByCamera] = useState<Record<string, boolean>>({});
  const [coordinatedTracking, setCoordinatedTracking] = useState(false);
  const [trackGain, setTrackGain] = useState(160);
  const [trackMaxSpeed, setTrackMaxSpeed] = useState(60);
  const [trackDeadzone, setTrackDeadzone] = useState(0.04);
  const [spatial, setSpatial] = useState<Record<string, CameraSpatial>>(() => Object.fromEntries(initialCameras.map((camera) => [camera.id, camera.spatial])));
  const [spatialSaved, setSpatialSaved] = useState(false);
  const [fallbackRangeM, setFallbackRangeM] = useState(20);
  const [streamAspect, setStreamAspect] = useState(DEFAULT_STREAM_ASPECT_RATIO);
  const dragRef = useRef<{ mode: "move" | "resize"; pointerId: number; startX: number; startY: number; frame: typeof frame; chromeHeight: number | null } | null>(null);
  const imageDragRef = useRef<{ cameraId: string; pointerId: number; x: number; y: number } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const previewWrapRef = useRef<HTMLDivElement>(null);
  const settingsDebounceRef = useRef<number | null>(null);
  const hasAutoSizedRef = useRef(false);

  useEffect(() => {
    setCameras(initialCameras);
    setSpatial((current) => Object.fromEntries(initialCameras.map((camera) => [camera.id, current[camera.id] ?? camera.spatial])));
  }, [initialCameras]);

  useEffect(() => {
    void getCameraTrackStates(initialCameras.map((camera) => camera.id)).then(setTrackingByCamera);
  }, [initialCameras]);

  useEffect(() => {
    void getCoordinatedCameraTrack().then(setCoordinatedTracking);
  }, []);

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
      setSpatial((current) => Object.fromEntries(next.map((camera) => [camera.id, current[camera.id] ?? camera.spatial])));
      setSelectedId((current) => next.some((camera) => camera.id === current) ? current : next[0]?.id ?? "");
    });
  };

  const changeSpatial = (cameraId: string, field: keyof CameraSpatial, value: number) => {
    setSpatial((current) => ({ ...current, [cameraId]: { ...current[cameraId], [field]: value } }));
    setSpatialSaved(false);
  };

  const saveSpatial = () => {
    void Promise.all([
      updateCameraSpatial(spatial),
      updateSettings({ coordinated_fallback_range_m: fallbackRangeM }),
    ])
      .then(() => setSpatialSaved(true))
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Camera spatial settings update failed"));
  };

  useEffect(() => {
    void fetchSettings().then((settings) => {
      if (typeof settings.coordinated_fallback_range_m === "number") setFallbackRangeM(settings.coordinated_fallback_range_m);
    });
  }, []);

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

  const toggleCameraTracking = (cameraId: string, enabled: boolean) => {
    setError(null);
    setTrackingByCamera((current) => ({ ...current, [cameraId]: enabled }));
    void setCameraTrack(cameraId, enabled)
      .then((actual) => {
        setTrackingByCamera((current) => ({ ...current, [cameraId]: actual }));
      })
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : "Track toggle failed");
        void getCameraTrack(cameraId).then((actual) => setTrackingByCamera((current) => ({ ...current, [cameraId]: actual })));
      });
  };

  const toggleCoordinatedTracking = (enabled: boolean) => {
    setCoordinatedTracking(enabled);
    void setCoordinatedCameraTrack(enabled)
      .then(setCoordinatedTracking)
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Coordinated tracking update failed"));
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

  const moveCamera = (cameraId: string, vector: PtzVector) => {
    setError(null);
    void sendAxisPtz(cameraId, vector.pan, vector.tilt, vector.zoom)
      .then(() => {
        // A manual move overrides auto-track server-side; the server owns the resulting state.
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

  const startImageDrag = (cameraId: string, event: ReactPointerEvent<HTMLImageElement>) => {
    if (!cameras.find((camera) => camera.id === cameraId)?.online) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    imageDragRef.current = { cameraId, pointerId: event.pointerId, x: event.clientX, y: event.clientY };
  };

  const moveImageDrag = (event: ReactPointerEvent<HTMLImageElement>) => {
    const drag = imageDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.x;
    const dy = drag.y - event.clientY;
    drag.x = event.clientX;
    drag.y = event.clientY;
    moveCamera(drag.cameraId, { pan: Math.max(-100, Math.min(100, dx * 3)), tilt: Math.max(-100, Math.min(100, dy * 3)), zoom: 0 });
  };

  const endImageDrag = (event: ReactPointerEvent<HTMLImageElement>) => {
    const drag = imageDragRef.current;
    if (drag?.pointerId === event.pointerId) {
      imageDragRef.current = null;
      moveCamera(drag.cameraId, PTZ_STOP);
    }
  };

  const zoomWithWheel = (cameraId: string, event: WheelEvent<HTMLImageElement>) => {
    event.preventDefault();
    moveCamera(cameraId, { pan: 0, tilt: 0, zoom: event.deltaY < 0 ? 70 : -70 });
    window.setTimeout(() => moveCamera(cameraId, PTZ_STOP), 120);
  };

  const hold = (cameraId: string, vector: PtzVector) => ({
    onPointerDown: (event: React.PointerEvent<HTMLButtonElement>) => {
      event.currentTarget.setPointerCapture(event.pointerId);
      moveCamera(cameraId, vector);
    },
    onPointerUp: () => moveCamera(cameraId, PTZ_STOP),
    onPointerCancel: () => moveCamera(cameraId, PTZ_STOP),
    onPointerLeave: () => moveCamera(cameraId, PTZ_STOP),
  });

  const renderPtzControls = (cameraId: string, compact = false) => (
    <div className={compact ? "camera-ptz-controls camera-all-ptz-controls" : "camera-ptz-controls"} aria-label={`${cameraId} PTZ controls`}>
      <button className="camera-ptz-up" title="Tilt up" {...hold(cameraId, { pan: 0, tilt: 70, zoom: 0 })}><ChevronUp size={compact ? 14 : 18} /></button>
      <button className="camera-ptz-left" title="Pan left" {...hold(cameraId, { pan: -70, tilt: 0, zoom: 0 })}><ChevronLeft size={compact ? 14 : 18} /></button>
      <button className="camera-ptz-stop" title="Stop movement" onClick={() => moveCamera(cameraId, PTZ_STOP)}><Square size={compact ? 10 : 13} /></button>
      <button className="camera-ptz-right" title="Pan right" {...hold(cameraId, { pan: 70, tilt: 0, zoom: 0 })}><ChevronRight size={compact ? 14 : 18} /></button>
      <button className="camera-ptz-down" title="Tilt down" {...hold(cameraId, { pan: 0, tilt: -70, zoom: 0 })}><ChevronDown size={compact ? 14 : 18} /></button>
      <button className="camera-ptz-zoom-out" title="Zoom out" {...hold(cameraId, { pan: 0, tilt: 0, zoom: -70 })}><Minus size={compact ? 12 : 16} /></button>
      <button className="camera-ptz-zoom-in" title="Zoom in" {...hold(cameraId, { pan: 0, tilt: 0, zoom: 70 })}><Plus size={compact ? 12 : 16} /></button>
    </div>
  );

  return (
    <section className="camera-panel" aria-label="Axis cameras" style={{ left: frame.x, top: frame.y, width: frame.width, height: frame.height }}>
      <header className="camera-panel-header" onPointerDown={(event) => startWindowDrag("move", event)} onPointerMove={moveWindowDrag} onPointerUp={endWindowDrag}>
        <div className="panel-title"><Camera size={17} /><strong>YP Cameras</strong></div>
        <button className="icon-button" title="Close cameras" onPointerDown={(event) => event.stopPropagation()} onClick={onClose}><X size={17} /></button>
      </header>
      <div className="camera-tabs" role="tablist" aria-label="Camera panel sections">
        <button
          role="tab"
          aria-selected={tab === "spatial"}
          className={tab === "spatial" ? "camera-tab active" : "camera-tab"}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={() => setTab("spatial")}
        >
          Spatial setup
        </button>
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
          aria-selected={tab === "all"}
          className={tab === "all" ? "camera-tab active" : "camera-tab"}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={() => setTab("all")}
        >
          All cameras
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
        {tab === "spatial" && (
          <div className="camera-spatial-form">
            <p className="camera-field-hint">Positions use meters relative to the YP reference point: X forward, Y starboard, Z up.</p>
            <div className="camera-field">
              <label htmlFor="coordinated-fallback-range">Single-camera acquisition range (m)</label>
              <p className="camera-field-hint">Temporary range used until a second camera sees the same target.</p>
              <input id="coordinated-fallback-range" type="number" min="1" step="1" value={fallbackRangeM} onChange={(event) => setFallbackRangeM(Number(event.target.value))} onPointerDown={(event) => event.stopPropagation()} />
            </div>
            {cameras.map((camera) => {
              const values = spatial[camera.id] ?? camera.spatial;
              return <fieldset className="camera-spatial-group" key={camera.id}>
                <legend>{camera.label}</legend>
                <div className="camera-spatial-grid">
                  {(["x_m", "y_m", "z_m", "heading_deg", "tilt_deg", "pan_zero_deg", "hfov_deg", "vfov_deg"] as const).map((field) => (
                      <label key={field}>{field.replace("_", " ")}
                      <input type="number" step="0.1" value={values[field]} onChange={(event) => changeSpatial(camera.id, field, Number(event.target.value))} onPointerDown={(event) => event.stopPropagation()} />
                    </label>
                  ))}
                </div>
              </fieldset>;
            })}
            <button className="camera-spatial-save" onClick={saveSpatial} onPointerDown={(event) => event.stopPropagation()}>Save spatial setup</button>
            {spatialSaved && <span className="camera-spatial-saved">Saved</span>}
          </div>
        )}
        {tab === "all" && cameras.length === 0 && <p className="camera-panel-empty">No Axis cameras configured.</p>}
        {tab === "all" && cameras.length > 0 && (
          <>
            <div className="camera-field camera-all-coordination">
              <label className="camera-toggle">
                <input type="checkbox" checked={coordinatedTracking} onChange={(event) => toggleCoordinatedTracking(event.target.checked)} onPointerDown={(event) => event.stopPropagation()} />
                <span>{coordinatedTracking ? "Coordinated tracking on" : "Coordinate enabled cameras"}</span>
              </label>
              <p className="camera-field-hint">Uses spatial setup to pan cameras checked for tracking toward the same two-camera fused target.</p>
            </div>
            <div className="camera-all-grid">
              {cameras.map((camera) => (
                <article className="camera-all-card" key={camera.id}>
                <div className="camera-all-card-header">
                  <strong>{camera.label}</strong>
                  <span className={camera.online ? "camera-status online" : "camera-status"}>{camera.online ? "Online" : "Offline"}</span>
                </div>
                <div className="camera-all-preview">
                  {camera.online ? (
                    <div className="camera-all-video-wrap">
                      <img
                        src={camera.stream_url}
                        alt={`${camera.label} live view`}
                        onPointerDown={(event) => startImageDrag(camera.id, event)}
                        onPointerMove={moveImageDrag}
                        onPointerUp={endImageDrag}
                        onPointerCancel={endImageDrag}
                        onWheel={(event) => zoomWithWheel(camera.id, event)}
                      />
                      {(() => {
                        const cameraDetections = detections?.[camera.id];
                        const fresh = !!cameraDetections && Date.now() / 1000 - cameraDetections.timestamp < DETECTION_MAX_AGE_SECONDS;
                        return fresh && cameraDetections ? (
                          <svg className="camera-detection-overlay" viewBox={`0 0 ${cameraDetections.frame_width} ${cameraDetections.frame_height}`} preserveAspectRatio="xMidYMid meet">
                            {cameraDetections.detections.map((detection, index) => {
                              const [x1, y1, x2, y2] = detection.box;
                              return <g key={index}>
                                <rect x={x1} y={y1} width={x2 - x1} height={y2 - y1} className="camera-detection-box" />
                                <text x={x1} y={Math.max(0, y1 - 4)} className="camera-detection-label">{detection.label} #{detection.track_id ?? "-"}</text>
                              </g>;
                            })}
                          </svg>
                        ) : null;
                      })()}
                    </div>
                  ) : <div className="camera-offline"><Camera size={22} />Unavailable</div>}
                </div>
                {camera.online && <label className="camera-toggle camera-all-track-toggle">
                  <input type="checkbox" checked={Boolean(trackingByCamera[camera.id])} onChange={(event) => toggleCameraTracking(camera.id, event.target.checked)} onPointerDown={(event) => event.stopPropagation()} />
                  <span>{trackingByCamera[camera.id] ? "Tracking on" : "Track this camera"}</span>
                </label>}
                <div className="camera-angle-readout">
                  <span>Ship relative</span>
                  <strong>{camera.ship_relative_deg === null ? "--" : `${camera.ship_relative_deg.toFixed(0)}°`}</strong>
                </div>
                {camera.online && camera.ptz_capable && renderPtzControls(camera.id, true)}
                </article>
              ))}
            </div>
          </>
        )}
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
            <div className="camera-angle-readout camera-angle-readout-selected">
              <span>Ship relative: <strong>{selected.ship_relative_deg === null ? "--" : `${selected.ship_relative_deg.toFixed(0)}°`}</strong></span>
              <span>Pan: <strong>{selected.pan_deg === null ? "--" : `${selected.pan_deg.toFixed(0)}°`}</strong></span>
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
                onPointerDown={(event) => startImageDrag(selected.id, event)}
                onPointerMove={moveImageDrag}
                onPointerUp={endImageDrag}
                onPointerCancel={endImageDrag}
                onWheel={(event) => zoomWithWheel(selected.id, event)}
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
                        {detection.label} #{detection.track_id ?? "-"}{detection.fusion_id ? ` / YP ${detection.fusion_id}` : ""} {Math.round(detection.confidence * 100)}%
                      </text>
                    </g>
                  );
                })}
              </svg>
            )}
          </div>
          {selected.online && selected.ptz_capable && (
            renderPtzControls(selected.id)
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