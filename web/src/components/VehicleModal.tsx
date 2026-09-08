import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { Brush, CircleDashed, Maximize2, RotateCcw, Route, Video, X } from "lucide-react";

import type { Vehicle, VehicleType } from "../types";
import { calculateRelativePosition } from "../utils/geo";
import styles from "./VehicleModal.module.css";

type VehicleModalProps = {
  vehicle: Vehicle;
  shipVehicle?: Vehicle;
  sarMissionActive?: boolean;
  canCommand?: boolean;
  onClose: () => void;
  onRtb: () => void;
  onEndSar: () => void;
  onWaypoint: () => void;
  onStreamVideo: () => void;
  onColorSave: (color: string) => void;
  onSetMode: (mode: string) => void;
};

const VEHICLE_MODES: Record<VehicleType, string[]> = {
  uav: ["STABILIZE", "ACRO", "ALT_HOLD", "AUTO", "GUIDED", "LOITER", "RTL", "CIRCLE", "LAND", "DRIFT", "SPORT", "FLIP", "AUTOTUNE", "POSHOLD"],
  usv: ["MANUAL", "GUIDED", "AUTO", "RTL", "LOITER", "CIRCLE"],
  ugv: ["MANUAL", "GUIDED", "AUTO", "RTL", "LOITER", "CIRCLE"],
  uavf: ["MANUAL", "ALTITUDE_CONTROL", "POSITION_CONTROL", "AUTO", "OFFBOARD", "EMERGENCY"],
  uuv: ["MANUAL", "GUIDED", "AUTO", "RTL", "LOITER"],
  yp: [],
};

const VEHICLE_COLOR_PALETTE = [
  "#dc2626", "#ef4444", "#f97316", "#f59e0b", "#eab308", "#84cc16",
  "#16a34a", "#14b8a6", "#06b6d4", "#0ea5e9", "#2563eb", "#4f46e5",
  "#7c3aed", "#c026d3", "#db2777", "#6b7280",
];

function vehicleMarkerColor(vehicle: Vehicle): string {
  const vehicleWithColor = vehicle as Vehicle & { marker_color?: string };
  return vehicleWithColor.marker_color ?? "#0ea5e9";
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className={styles.metric}><span>{label}</span><strong>{value}</strong></div>;
}

export function VehicleModal({
  vehicle,
  shipVehicle,
  sarMissionActive = false,
  canCommand = true,
  onClose,
  onRtb,
  onEndSar,
  onWaypoint,
  onStreamVideo,
  onColorSave,
  onSetMode,
}: VehicleModalProps) {
  const position = vehicle.position;
  const [showColorPalette, setShowColorPalette] = useState(false);
  const [showModeSelector, setShowModeSelector] = useState(false);
  const [draftColor, setDraftColor] = useState(vehicleMarkerColor(vehicle));
  const canStreamVideo = Boolean(vehicle.video?.enabled && ((Array.isArray(vehicle.video?.streams) && vehicle.video.streams.length > 0) || Boolean(vehicle.video?.playback_url)));
  const modalRef = useRef<HTMLDivElement | null>(null);
  const relativePos = shipVehicle && vehicle.vehicle_id !== shipVehicle.vehicle_id ? calculateRelativePosition(shipVehicle, vehicle) : null;
  const [frame, setFrame] = useState(() => ({ x: 20, y: Math.max(20, window.innerHeight - 480), width: Math.min(340, Math.max(280, window.innerWidth - 24)) }));

  const clampFrameToViewport = (candidate: typeof frame) => {
    const padding = 12;
    const maxWidth = Math.max(280, Math.min(600, window.innerWidth - padding * 2));
    const width = Math.min(maxWidth, Math.max(280, candidate.width));
    const measuredHeight = modalRef.current?.offsetHeight ?? 420;
    const maxX = Math.max(padding, window.innerWidth - width - padding);
    const maxY = Math.max(padding, window.innerHeight - measuredHeight - padding);
    return { width, x: Math.min(Math.max(padding, candidate.x), maxX), y: Math.min(Math.max(padding, candidate.y), maxY) };
  };

  useEffect(() => { setFrame((current) => clampFrameToViewport(current)); }, [vehicle.vehicle_id, showColorPalette, showModeSelector]);
  useEffect(() => {
    const onResize = () => setFrame((current) => clampFrameToViewport(current));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const dragRef = useRef<{ mode: "move" | "resize"; pointerId: number; startX: number; startY: number; frame: typeof frame } | null>(null);
  const startDrag = (mode: "move" | "resize", event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault(); event.stopPropagation(); event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { mode, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, frame };
  };
  const moveDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    setFrame(clampFrameToViewport({ ...drag.frame, x: drag.mode === "move" ? drag.frame.x + dx : drag.frame.x, y: drag.mode === "move" ? drag.frame.y + dy : drag.frame.y, width: drag.mode === "resize" ? drag.frame.width + dx : drag.frame.width }));
  };
  const endDrag = (event: ReactPointerEvent<HTMLElement>) => { if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null; };

  return (
    <div ref={modalRef} className={styles.vehicleModal} style={{ position: "fixed", left: frame.x, top: frame.y, width: frame.width, margin: 0, zIndex: 5000, boxShadow: "0 10px 25px -5px rgba(0, 0, 0, 0.5)", cursor: "default", maxHeight: "calc(100vh - 24px)", overflowY: "auto", overscrollBehavior: "contain" }} onMouseDown={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()} onClick={(event) => event.stopPropagation()}>
      <div className={styles.modalHeader} style={{ cursor: "grab" }} onPointerDown={(event) => startDrag("move", event)} onPointerMove={moveDrag} onPointerUp={endDrag}>
        <div className={`${styles.typeChip} ${styles[vehicle.vehicle_type]}`}>{vehicle.vehicle_type.toUpperCase()}</div>
        <div style={{ flex: 1 }}><h2>{vehicle.vehicle_id}</h2><p>{vehicle.connected ? "Connected" : "Last seen offline"}</p></div>
        <button className="icon-button" title="Close" onPointerDown={(event) => event.stopPropagation()} onClick={onClose}><X size={20} /></button>
      </div>
      <div className={styles.metrics}>
        <Metric label="Latitude" value={position?.latitude.toFixed(6) ?? "--"} /><Metric label="Longitude" value={position?.longitude.toFixed(6) ?? "--"} /><Metric label="Altitude" value={`${(position?.altitude ?? 0).toFixed(1)} m`} /><Metric label="Heading" value={`${(vehicle.heading ?? 0).toFixed(0)} deg`} /><Metric label="Battery" value={vehicle.battery?.percentage == null ? "--" : `${Math.round(vehicle.battery.percentage * 100)}%`} /><Metric label="SAR Mission" value={sarMissionActive ? "Running" : "Idle"} />
      </div>
      {relativePos && <div style={{ marginTop: "15px", paddingTop: "15px", borderTop: "1px solid #334155" }}><div style={{ fontSize: "12px", fontWeight: "bold", color: "#94a3b8", marginBottom: "8px", textTransform: "uppercase" }}>Ship Reference Frame (FLU)</div><div className={styles.metrics}><Metric label="X (Forward)" value={`${relativePos.x > 0 ? "+" : ""}${relativePos.x.toFixed(1)} m`} /><Metric label="Y (Left/Port)" value={`${relativePos.y > 0 ? "+" : ""}${relativePos.y.toFixed(1)} m`} /><Metric label="Z (Up)" value={`${relativePos.z > 0 ? "+" : ""}${relativePos.z.toFixed(1)} m`} /><Metric label="Radial Dist." value={`${relativePos.distance.toFixed(1)} m`} /></div></div>}
      <div className={styles.modalActions} style={{ marginTop: "15px" }}>
        {canCommand && <button className={styles.secondary} onClick={onEndSar} disabled={!sarMissionActive} title={sarMissionActive ? "Stop active SAR mission" : "No active SAR mission"}><CircleDashed size={18} />End SAR Mission</button>}
        {canCommand && <button className={styles.danger} onClick={onRtb}><RotateCcw size={18} />RTB</button>}
        <button className={styles.secondary} onClick={() => setShowColorPalette((value) => !value)}><Brush size={18} />Color</button>
        {canCommand && VEHICLE_MODES[vehicle.vehicle_type]?.length > 0 && <button className={styles.secondary} onClick={() => setShowModeSelector((value) => !value)}>Settings{showModeSelector && <X size={14} aria-label="Close mode selector" />}</button>}
        {canStreamVideo && <button className={styles.stream} onClick={onStreamVideo}><Video size={18} />Stream Video</button>}
        {canCommand && <button className={styles.primary} onClick={onWaypoint}><Route size={18} />Waypoint</button>}
      </div>
      {showColorPalette && <div className={styles.colorPanel}><div className={styles.colorSwatches}>{VEHICLE_COLOR_PALETTE.map((color) => <button key={color} className={`${styles.colorSwatch} ${draftColor === color ? styles.selected : ""}`} style={{ backgroundColor: color }} title={color} onClick={() => { setDraftColor(color); onColorSave(color); }} />)}</div></div>}
      {showModeSelector && VEHICLE_MODES[vehicle.vehicle_type]?.length > 0 && <div className={styles.colorPanel}><div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>{VEHICLE_MODES[vehicle.vehicle_type].map((mode) => <button key={mode} className={styles.secondary} style={{ fontSize: "13px", padding: "6px 8px" }} onClick={() => { onSetMode(mode); setShowModeSelector(false); }}>{mode}</button>)}</div></div>}
      <div style={{ position: "absolute", bottom: 0, right: 0, width: "24px", height: "24px", cursor: "nwse-resize", display: "flex", alignItems: "flex-end", justifyContent: "flex-end", padding: "4px" }} onPointerDown={(event) => startDrag("resize", event)} onPointerMove={moveDrag} onPointerUp={endDrag}><Maximize2 size={14} color="#64748b" style={{ transform: "rotate(90deg)" }} /></div>
    </div>
  );
}
