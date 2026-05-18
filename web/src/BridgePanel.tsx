/**
 * BridgePanel — Lifeguard mission control panel for the bridge display.
 *
 * Operators can:
 *   • Tap two map corners to define a grid-search rectangle, then dispatch it.
 *   • Tap the MOB button to immediately launch a MOB parallel-track search.
 *   • Fly an agent to a position previously picked on the map.
 *   • Monitor per-agent connection and mission status.
 *   • Review a live status message log.
 */
import React, { useState } from "react";
import { AlertTriangle, Navigation, Map, X } from "lucide-react";
import type { LifeguardAgent, LifeguardStatus, LifeguardConfig } from "./types";
import { sendLifeguardCommand } from "./api";

// ---------------------------------------------------------------------------
// Haversine helper (metres between two lat/lon points)
// ---------------------------------------------------------------------------
function haversineM(
  lat1: number,
  lon1: number,
  lat2: number,
  lon2: number,
): number {
  const R = 6_378_137.0;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dlat = toRad(lat2 - lat1);
  const dlon = toRad(lon2 - lon1);
  const a =
    Math.sin(dlat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dlon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
export interface BridgePanelProps {
  ws: WebSocket | null;
  agents: LifeguardAgent[];
  statusLog: LifeguardStatus[];
  config: LifeguardConfig | null;
  /** Null corners mean no corner is being picked. */
  corner1: [number, number] | null;
  corner2: [number, number] | null;
  pickMode: "corner1" | "corner2" | null;
  onPickMode: (mode: "corner1" | "corner2" | null) => void;
  onClearCorners: () => void;
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
export default function BridgePanel({
  ws,
  agents,
  statusLog,
  config,
  corner1,
  corner2,
  pickMode,
  onPickMode,
  onClearCorners,
  onClose,
}: BridgePanelProps) {
  const defaultAlt = config?.mission.default_waypoint_altitude ?? 30;
  const defaultSwath = config?.mission.default_swath_width ?? 20;

  const [selectedAgent, setSelectedAgent] = useState<string>(
    agents.find((a) => a.connected)?.id ?? "",
  );
  const [altitude, setAltitude] = useState<number>(defaultAlt);
  const [swath, setSwath] = useState<number>(defaultSwath);

  // Keep the selected agent in sync when agent list updates.
  const agentIds = agents.map((a) => a.id);
  if (selectedAgent && !agentIds.includes(selectedAgent)) {
    setSelectedAgent(agents.find((a) => a.connected)?.id ?? "");
  }

  // Compute grid parameters from the two corner picks.
  const gridReady = corner1 !== null && corner2 !== null;
  const gridSizeM = gridReady
    ? Math.max(
        haversineM(corner1![0], corner1![1], corner2![0], corner1![1]),
        haversineM(corner1![0], corner1![1], corner1![0], corner2![1]),
      )
    : null;
  const gridCenterLat = gridReady ? (corner1![0] + corner2![0]) / 2 : null;
  const gridCenterLon = gridReady ? (corner1![1] + corner2![1]) / 2 : null;

  function dispatchGrid() {
    if (!gridReady || !selectedAgent) return;
    sendLifeguardCommand(ws, {
      command: "grid_search",
      agent_id: selectedAgent,
      lat: gridCenterLat,
      lon: gridCenterLon,
      grid_size_m: Math.round(gridSizeM!),
      swath_m: swath,
      altitude_m: altitude,
    });
    onClearCorners();
  }

  function dispatchMob() {
    sendLifeguardCommand(ws, { command: "mob" });
  }

  function levelClass(level: string) {
    if (level === "error") return "bridge-status-error";
    if (level === "warn") return "bridge-status-warn";
    return "bridge-status-info";
  }

  return (
    <div className="bridge-panel">
      {/* Header */}
      <div className="bridge-header">
        <span className="bridge-title">
          <Navigation size={16} /> Bridge — Lifeguard Control
        </span>
        <button className="bridge-close" onClick={onClose} aria-label="Close">
          <X size={16} />
        </button>
      </div>

      {/* MOB — always the most prominent control */}
      <div className="bridge-section bridge-mob-section">
        <button className="mob-button" onClick={dispatchMob}>
          <AlertTriangle size={22} />
          MOB — Man Overboard
        </button>
        <p className="bridge-hint">
          Dispatches the first idle agent on a parallel-track search along the
          ship's recent track.
        </p>
      </div>

      {/* Grid search */}
      <div className="bridge-section">
        <h3 className="bridge-section-title">
          <Map size={14} /> Grid Search
        </h3>

        <div className="bridge-row">
          <button
            className={`bridge-pick-btn ${pickMode === "corner1" ? "active" : ""}`}
            onClick={() => onPickMode(pickMode === "corner1" ? null : "corner1")}
          >
            {corner1
              ? `Corner 1: ${corner1[0].toFixed(4)}, ${corner1[1].toFixed(4)}`
              : "Tap Corner 1"}
          </button>
          <button
            className={`bridge-pick-btn ${pickMode === "corner2" ? "active" : ""}`}
            onClick={() => onPickMode(pickMode === "corner2" ? null : "corner2")}
          >
            {corner2
              ? `Corner 2: ${corner2[0].toFixed(4)}, ${corner2[1].toFixed(4)}`
              : "Tap Corner 2"}
          </button>
          {(corner1 || corner2) && (
            <button className="bridge-clear-btn" onClick={onClearCorners}>
              Clear
            </button>
          )}
        </div>

        {gridReady && (
          <p className="bridge-hint">
            Grid size: {gridSizeM!.toFixed(0)} m &nbsp;|&nbsp; Centre:{" "}
            {gridCenterLat!.toFixed(5)}, {gridCenterLon!.toFixed(5)}
          </p>
        )}

        <div className="bridge-row bridge-inputs">
          <label>
            Alt (m)
            <input
              type="number"
              min={5}
              max={500}
              value={altitude}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setAltitude(Number(e.target.value))}
            />
          </label>
          <label>
            Swath (m)
            <input
              type="number"
              min={1}
              max={200}
              value={swath}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSwath(Number(e.target.value))}
            />
          </label>
        </div>

        <div className="bridge-row bridge-inputs">
          <label>
            Agent
            <select
              value={selectedAgent}
              onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setSelectedAgent(e.target.value)}
            >
              {agents.length === 0 && (
                <option value="">— no agents —</option>
              )}
              {agents.map((a) => (
                <option key={a.id} value={a.id} disabled={!a.connected}>
                  {a.id}
                  {!a.connected ? " (offline)" : a.active_mission ? ` (${a.active_mission})` : ""}
                </option>
              ))}
            </select>
          </label>
        </div>

        <button
          className="bridge-dispatch-btn"
          disabled={!gridReady || !selectedAgent}
          onClick={dispatchGrid}
        >
          Dispatch Grid Search
        </button>
      </div>

      {/* Agent status */}
      <div className="bridge-section">
        <h3 className="bridge-section-title">Agents</h3>
        {agents.length === 0 ? (
          <p className="bridge-hint">No agents configured.</p>
        ) : (
          <ul className="bridge-agent-list">
            {agents.map((a) => (
              <li key={a.id} className="bridge-agent-row">
                <span
                  className={`agent-indicator ${a.connected ? "connected" : "disconnected"}`}
                />
                <span className="agent-name">{a.id}</span>
                <span className="agent-type">{a.frame_type}</span>
                <span className="agent-mission">
                  {a.active_mission ?? "idle"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Status log */}
      <div className="bridge-section bridge-log-section">
        <h3 className="bridge-section-title">Status Log</h3>
        <ul className="bridge-status-log">
          {statusLog.length === 0 && (
            <li className="bridge-status-info bridge-status-entry">No messages yet.</li>
          )}
          {[...statusLog].reverse().map((s, i) => (
            <li key={i} className={`bridge-status-entry ${levelClass(s.level)}`}>
              <span className="log-time">
                {new Date(s.stamp * 1000).toLocaleTimeString()}
              </span>{" "}
              {s.agent_id ? `[${s.agent_id}] ` : ""}
              {s.message}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
