import L from "leaflet";
import {
  AlertTriangle,
  Battery,
  Crosshair,
  EthernetPort,
  LocateFixed,
  MessageSquare,
  RotateCcw,
  Route,
  Settings,
  Ship,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { CircleMarker, MapContainer, Marker, Polyline, Popup, Rectangle, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";

import { sendCommand, sendLifeguardCommand, websocketUrl } from "./api";
import BridgePanel from "./BridgePanel";
import type { Command, LifeguardAgent, LifeguardConfig, LifeguardStatus, Vehicle, VehicleType } from "./types";

const USNA_CENTER: [number, number] = [38.9822, -76.4819];
const MAX_MESSAGE_LOG = 700;

type MapBase = "satellite" | "street";
type MapSource = "auto" | "cache" | "online";

interface StreamMessage {
  id: string;
  receivedAt: number;
  vehicle_id: string;
  vehicle_type: VehicleType;
  topic: string;
  type: string;
  stamp: number;
  msg: Record<string, unknown>;
}

export function App() {
  const [vehicles, setVehicles] = useState<Record<string, Vehicle>>({});
  const [connected, setConnected] = useState(false);
  const [selected, setSelected] = useState<Vehicle | null>(null);
  const [pendingWaypointFor, setPendingWaypointFor] = useState<string | null>(null);
  const [trailSeconds, setTrailSeconds] = useState(30);
  const [showSettings, setShowSettings] = useState(false);
  const [showMessages, setShowMessages] = useState(false);
  const [messagePanelWidth, setMessagePanelWidth] = useState(500);
  const [topicFilters, setTopicFilters] = useState<string[]>([]);
  const [messageLog, setMessageLog] = useState<StreamMessage[]>([]);
  const [mapBase, setMapBase] = useState<MapBase>("satellite");
  const [mapSource, setMapSource] = useState<MapSource>("auto");
  const [followYp, setFollowYp] = useState(true);

  // Lifeguard / Bridge panel state
  const [showBridgePanel, setShowBridgePanel] = useState(false);
  const [lifeguardAgents, setLifeguardAgents] = useState<LifeguardAgent[]>([]);
  const [lifeguardConfig, setLifeguardConfig] = useState<LifeguardConfig | null>(null);
  const [lifeguardStatusLog, setLifeguardStatusLog] = useState<LifeguardStatus[]>([]);
  const [bridgePickMode, setBridgePickMode] = useState<"corner1" | "corner2" | null>(null);
  const [bridgeCorner1, setBridgeCorner1] = useState<[number, number] | null>(null);
  const [bridgeCorner2, setBridgeCorner2] = useState<[number, number] | null>(null);

  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let retry: number | undefined;

    const connect = () => {
      const ws = new WebSocket(websocketUrl("/ws/ui"));
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        retry = window.setTimeout(connect, 1500);
      };
      ws.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (payload.op === "snapshot") {
          setVehicles(Object.fromEntries(payload.vehicles.map((vehicle: Vehicle) => [vehicle.vehicle_id, vehicle])));
        }
        if (payload.op === "vehicle_update") {
          setVehicles((current) => ({ ...current, [payload.vehicle.vehicle_id]: payload.vehicle }));
          if (payload.message) {
            setMessageLog((current) => [streamMessageFromPayload(payload.message), ...current].slice(0, MAX_MESSAGE_LOG));
          }
        }
        if (payload.op === "command_ack") {
          setMessageLog((current) => [streamMessageFromCommandAck(payload), ...current].slice(0, MAX_MESSAGE_LOG));
        }
        if (payload.op === "vehicle_disconnected") {
          setVehicles((current) => ({
            ...current,
            [payload.vehicle_id]: {
              ...current[payload.vehicle_id],
              connected: false,
            },
          }));
        }
        // Lifeguard events
        if (payload.op === "lifeguard_agents") {
          setLifeguardAgents(payload.agents ?? []);
        }
        if (payload.op === "lifeguard_status") {
          setLifeguardStatusLog((prev) => [...prev, payload as LifeguardStatus].slice(-200));
        }
        if (payload.op === "lifeguard_config") {
          setLifeguardConfig(payload.config ?? null);
        }
      };
    };

    connect();
    return () => {
      window.clearTimeout(retry);
      wsRef.current?.close();
    };
  }, []);

  const vehicleList = useMemo(() => Object.values(vehicles).filter((vehicle) => vehicle.position), [vehicles]);
  const yp = vehicleList.find((vehicle) => vehicle.vehicle_type === "yp");
  const ypGpsLinked = Boolean(yp?.connected);
  const center: [number, number] = yp?.position ? [yp.position.latitude, yp.position.longitude] : USNA_CENTER;
  const filteredMessages = useMemo(() => filterMessages(messageLog, topicFilters), [messageLog, topicFilters]);
  const mapLayer = useMemo(() => tileLayerFor(mapBase, mapSource), [mapBase, mapSource]);

  const command = (vehicleId: string, body: Command) => sendCommand(wsRef.current, vehicleId, body);

  // When bridge pick mode is active, map clicks set corners instead of waypoints.
  const handleMapClick = (lat: number, lon: number) => {
    if (bridgePickMode === "corner1") {
      setBridgeCorner1([lat, lon]);
      setBridgePickMode("corner2");
    } else if (bridgePickMode === "corner2") {
      setBridgeCorner2([lat, lon]);
      setBridgePickMode(null);
    }
  };

  return (
    <div className={pendingWaypointFor || bridgePickMode ? "app picking" : "app"}>
      <MapContainer center={center} zoom={15} minZoom={3} maxZoom={19} zoomControl className="map">
        <TileLayer key={`${mapBase}-${mapSource}`} url={mapLayer.url} attribution={mapLayer.attribution} />
        <MapCommander
          pendingWaypointFor={bridgePickMode ? null : pendingWaypointFor}
          selectedVehicle={pendingWaypointFor && !bridgePickMode ? vehicles[pendingWaypointFor] : null}
          onWaypoint={(vehicleId, lat, lon, altitude) => {
            command(vehicleId, { type: "waypoint", target: { latitude: lat, longitude: lon, altitude } });
            setPendingWaypointFor(null);
          }}
          onMapClick={bridgePickMode ? handleMapClick : undefined}
        />
        <MapPanTracker onManualPan={() => setFollowYp(false)} />
        <FollowYpCenter yp={yp} enabled={followYp} />
        <FitAllControl vehicles={vehicleList} />
        {vehicleList.map((vehicle) => (
          <VehicleLayer
            key={vehicle.vehicle_id}
            vehicle={vehicle}
            trailSeconds={trailSeconds}
            onClick={() => {
              if (vehicle.vehicle_type === "yp") {
                setFollowYp(true);
              }
              setSelected(vehicle);
            }}
          />
        ))}
        {/* Grid-search rectangle preview */}
        {bridgeCorner1 && bridgeCorner2 && (
          <Rectangle
            bounds={[bridgeCorner1, bridgeCorner2]}
            pathOptions={{ color: "#f59e0b", weight: 2, fillOpacity: 0.1 }}
          />
        )}
      </MapContainer>

      <MapMenu mapBase={mapBase} mapSource={mapSource} onMapBaseChange={setMapBase} onMapSourceChange={setMapSource} />

      <div className="topbar">
        <div className="brand">
          <img className="brand-logo" src="/logos/usna_crest_jhublue.png" alt="USNA crest" />
          <div className="brand-copy">
            <strong>YP Vehicle View</strong>
            <div className="brand-statuses">
              <span className={connected ? "brand-status online" : "brand-status offline"}>
                <EthernetPort size={15} />
                {connected ? "Server linked" : "Server offline"}
              </span>
              <span className={ypGpsLinked ? "brand-status online" : "brand-status offline"}>
                {ypGpsLinked ? <Wifi size={15} /> : <WifiOff size={15} />}
                {ypGpsLinked ? "YP GPS linked" : "YP GPS offline"}
              </span>
              <span className={vehicleList.length > 0 ? "brand-status online" : "brand-status offline"}>
                <Ship size={15} />
                {vehicleList.length} tracked
              </span>
            </div>
          </div>
        </div>
        <div className="topbar-actions">
          <button
            className={showBridgePanel ? "icon-button active" : "icon-button"}
            title="Bridge Panel — Lifeguard Control"
            onClick={() => setShowBridgePanel((v) => !v)}
          >
            <AlertTriangle size={19} />
          </button>
          <button className="icon-button" title="Settings" onClick={() => setShowSettings((value) => !value)}>
            <Settings size={19} />
          </button>
          <button className="icon-button" title="Messages" onClick={() => setShowMessages((value) => !value)}>
            <MessageSquare size={19} />
          </button>
        </div>
      </div>

      {showSettings && (
        <div className="settings-panel">
          <div className="panel-title">
            <Settings size={17} />
            <strong>Settings</strong>
          </div>
          <label>
            Trail window
            <span>{trailSeconds}s</span>
          </label>
          <input min={5} max={300} step={5} type="range" value={trailSeconds} onChange={(event) => setTrailSeconds(Number(event.target.value))} />
        </div>
      )}

      {bridgePickMode && (
        <div className="target-banner">
          <Crosshair size={18} />
          <span>
            {bridgePickMode === "corner1" ? "Tap the map to set Corner 1" : "Tap the map to set Corner 2"}
          </span>
          <button title="Cancel" onClick={() => setBridgePickMode(null)}>
            <X size={17} />
          </button>
        </div>
      )}

      {showBridgePanel && (
        <BridgePanel
          ws={wsRef.current}
          agents={lifeguardAgents}
          statusLog={lifeguardStatusLog}
          config={lifeguardConfig}
          corner1={bridgeCorner1}
          corner2={bridgeCorner2}
          pickMode={bridgePickMode}
          onPickMode={setBridgePickMode}
          onClearCorners={() => { setBridgeCorner1(null); setBridgeCorner2(null); setBridgePickMode(null); }}
          onClose={() => setShowBridgePanel(false)}
        />
      )}

      {pendingWaypointFor && (
        <div className="target-banner">
          <Crosshair size={18} />
          <span>Click the map to send {pendingWaypointFor} a waypoint</span>
          <button title="Cancel waypoint" onClick={() => setPendingWaypointFor(null)}>
            <X size={17} />
          </button>
        </div>
      )}

      {showMessages && (
        <MessageDrawer
          messages={messageLog}
          filteredMessages={filteredMessages}
          filters={topicFilters}
          width={messagePanelWidth}
          onClose={() => setShowMessages(false)}
          onResize={setMessagePanelWidth}
          onFiltersChange={setTopicFilters}
        />
      )}

      {selected && (
        <VehicleModal
          vehicle={selected}
          onClose={() => setSelected(null)}
          onRtb={() => {
            command(selected.vehicle_id, { type: "rtb" });
            setSelected(null);
          }}
          onWaypoint={() => {
            setPendingWaypointFor(selected.vehicle_id);
            setSelected(null);
          }}
        />
      )}
    </div>
  );
}

function streamMessageFromPayload(payload: {
  vehicle_id?: string;
  vehicle_type?: VehicleType;
  topic?: string;
  type?: string;
  stamp?: number;
  msg?: Record<string, unknown>;
}): StreamMessage {
  const receivedAt = Date.now();
  return {
    id: `${payload.topic ?? "message"}-${payload.stamp ?? receivedAt}-${receivedAt}`,
    receivedAt,
    vehicle_id: payload.vehicle_id ?? topicParts(payload.topic ?? "")[1] ?? "unknown",
    vehicle_type: payload.vehicle_type ?? "uav",
    topic: payload.topic ?? "/unknown",
    type: payload.type ?? "unknown",
    stamp: payload.stamp ?? receivedAt / 1000,
    msg: payload.msg ?? {},
  };
}

function streamMessageFromCommandAck(payload: {
  vehicle_id?: string;
  stamp?: number;
  command?: Record<string, unknown>;
  delivered?: boolean;
  source?: string;
}): StreamMessage {
  const receivedAt = Date.now();
  const vehicleId = payload.vehicle_id ?? "unknown";
  return {
    id: `/vehicles/${vehicleId}/commands-${payload.stamp ?? receivedAt}-${receivedAt}`,
    receivedAt,
    vehicle_id: vehicleId,
    vehicle_type: "uav",
    topic: `/vehicles/${vehicleId}/commands`,
    type: "yp_ground_station/CommandAck",
    stamp: payload.stamp ?? receivedAt / 1000,
    msg: {
      delivered: payload.delivered,
      source: payload.source,
      command: payload.command,
    },
  };
}

function topicParts(topic: string): string[] {
  return topic.split("/").filter(Boolean);
}

function filterMessages(messages: StreamMessage[], filters: string[]): StreamMessage[] {
  const activeFilters = filters.filter((filter) => filter !== "all");
  if (activeFilters.length === 0) {
    return messages;
  }
  return messages.filter((message) => {
    const parts = topicParts(message.topic);
    return activeFilters.every((filter, index) => parts[index] === filter);
  });
}

function topicOptions(messages: StreamMessage[], filters: string[], depth: number): string[] {
  const values = new Set<string>();
  for (const message of messages) {
    const parts = topicParts(message.topic);
    const matchesPrefix = filters.slice(0, depth).every((filter, index) => filter === "all" || parts[index] === filter);
    if (matchesPrefix && parts[depth]) {
      values.add(parts[depth]);
    }
  }
  return Array.from(values).sort((a, b) => a.localeCompare(b));
}

function filterLabel(depth: number): string {
  return ["Topic root", "Vehicle ID", "Message topic", "Subtopic"][depth] ?? `Level ${depth + 1}`;
}

function tileLayerFor(base: MapBase, source: MapSource): { url: string; attribution: string } {
  if (base === "street") {
    return {
      auto: {
        url: "/tiles/osm/{z}/{x}/{y}.png",
        attribution: "&copy; OpenStreetMap contributors",
      },
      cache: {
        url: "/tiles/cache/{z}/{x}/{y}.png",
        attribution: "&copy; OpenStreetMap contributors",
      },
      online: {
        url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        attribution: "&copy; OpenStreetMap contributors",
      },
    }[source];
  }

  return {
    auto: {
      url: "/tiles/earth/{z}/{x}/{y}.png",
      attribution: "Tiles &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    },
    cache: {
      url: "/tiles/earth-cache/{z}/{x}/{y}.png",
      attribution: "Tiles &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    },
    online: {
      url: "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      attribution: "Tiles &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    },
  }[source];
}

function MapMenu({
  mapBase,
  mapSource,
  onMapBaseChange,
  onMapSourceChange,
}: {
  mapBase: MapBase;
  mapSource: MapSource;
  onMapBaseChange: (base: MapBase) => void;
  onMapSourceChange: (source: MapSource) => void;
}) {
  return (
    <div className="map-menu" aria-label="Map options">
      <fieldset>
        <legend>Map</legend>
        <label>
          <input type="radio" name="map-base" value="satellite" checked={mapBase === "satellite"} onChange={() => onMapBaseChange("satellite")} />
          Satellite
        </label>
        <label>
          <input type="radio" name="map-base" value="street" checked={mapBase === "street"} onChange={() => onMapBaseChange("street")} />
          Street Maps
        </label>
      </fieldset>
      <fieldset>
        <legend>Source</legend>
        <label>
          <input type="radio" name="map-source" value="auto" checked={mapSource === "auto"} onChange={() => onMapSourceChange("auto")} />
          Auto
        </label>
        <label>
          <input type="radio" name="map-source" value="cache" checked={mapSource === "cache"} onChange={() => onMapSourceChange("cache")} />
          Cached only
        </label>
        <label>
          <input type="radio" name="map-source" value="online" checked={mapSource === "online"} onChange={() => onMapSourceChange("online")} />
          Online only
        </label>
      </fieldset>
    </div>
  );
}

function MessageDrawer({
  messages,
  filteredMessages,
  filters,
  width,
  onClose,
  onResize,
  onFiltersChange,
}: {
  messages: StreamMessage[];
  filteredMessages: StreamMessage[];
  filters: string[];
  width: number;
  onClose: () => void;
  onResize: (width: number) => void;
  onFiltersChange: (filters: string[]) => void;
}) {
  const [selectedMessage, setSelectedMessage] = useState<StreamMessage | null>(null);
  const clampedWidth = Math.max(360, Math.min(width, Math.floor(window.innerWidth * 0.82)));

  const filterControls = useMemo(() => {
    const controls: Array<{ depth: number; options: string[]; value: string }> = [];
    for (let depth = 0; depth < 8; depth += 1) {
      const options = topicOptions(messages, filters, depth);
      const selected = filters[depth] ?? "all";
      if (options.length === 0) {
        break;
      }
      controls.push({ depth, options, value: selected });
      if (selected === "all") {
        break;
      }
    }
    return controls;
  }, [messages, filters]);

  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startWidth = clampedWidth;

    const move = (moveEvent: PointerEvent) => {
      onResize(Math.max(360, Math.min(startWidth + startX - moveEvent.clientX, window.innerWidth - 96)));
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const updateFilter = (depth: number, value: string) => {
    const next = filters.slice(0, depth);
    next[depth] = value;
    onFiltersChange(next);
  };

  return (
    <aside className="message-drawer" style={{ width: clampedWidth }}>
      <div className="resize-handle" onPointerDown={startResize} />
      <div className="message-header">
        <div className="panel-title">
          <MessageSquare size={18} />
          <strong>Messages</strong>
        </div>
        <div className="message-count">
          {filteredMessages.length} / {messages.length}
        </div>
        <button className="icon-button" title="Close messages" onClick={onClose}>
          <X size={19} />
        </button>
      </div>

      <div className="filter-stack">
        {filterControls.map((control) => (
          <label key={control.depth}>
            <span>{filterLabel(control.depth)}</span>
            <select value={control.value} onChange={(event) => updateFilter(control.depth, event.target.value)}>
              <option value="all">All</option>
              {control.options.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </label>
        ))}
      </div>

      <div className="message-list">
        {selectedMessage && (
          <article className="pinned-message">
            <div className="pinned-message-header">
              <div>
                <strong>{selectedMessage.topic}</strong>
                <span>{new Date(selectedMessage.receivedAt).toLocaleTimeString()}</span>
              </div>
              <button className="icon-button" title="Close message" onClick={() => setSelectedMessage(null)}>
                <X size={17} />
              </button>
            </div>
            <div className="message-row-meta">
              <span>{selectedMessage.type}</span>
              <span>{selectedMessage.vehicle_id}</span>
            </div>
            <pre>{JSON.stringify(selectedMessage.msg, null, 2)}</pre>
          </article>
        )}
        {filteredMessages.length === 0 ? (
          <div className="empty-messages">No messages match the current filter.</div>
        ) : (
          filteredMessages.map((message) => (
            <button
              key={message.id}
              className={selectedMessage?.id === message.id ? "message-row selected" : "message-row"}
              onClick={() => setSelectedMessage(selectedMessage?.id === message.id ? null : message)}
            >
              <div className="message-row-top">
                <strong>{message.topic}</strong>
                <span>{new Date(message.receivedAt).toLocaleTimeString()}</span>
              </div>
              <div className="message-row-meta">
                <span>{message.type}</span>
                <span>{message.vehicle_id}</span>
              </div>
            </button>
          ))
        )}
      </div>
    </aside>
  );
}

function VehicleLayer({ vehicle, trailSeconds, onClick }: { vehicle: Vehicle; trailSeconds: number; onClick: () => void }) {
  const position = vehicle.position!;
  const cutoff = Date.now() / 1000 - trailSeconds;
  const trail = (vehicle.history ?? [])
    .filter((point) => !point.stamp || point.stamp >= cutoff)
    .map((point) => [point.latitude, point.longitude] as [number, number]);
  const color = vehicleColor(vehicle.vehicle_type);

  return (
    <>
      {trail.length > 1 && <Polyline positions={trail} pathOptions={{ color, weight: 3, opacity: 0.75 }} />}
      <Marker
        position={[position.latitude, position.longitude]}
        icon={vehicleIcon(vehicle)}
        eventHandlers={{
          click: onClick,
        }}
      >
        <Tooltip direction="top" offset={[0, -18]}>
          <TelemetryTooltip vehicle={vehicle} />
        </Tooltip>
        <Popup>
          <TelemetryTooltip vehicle={vehicle} />
        </Popup>
      </Marker>
      {vehicle.vehicle_type === "yp" && (
        <CircleMarker center={[position.latitude, position.longitude]} radius={18} pathOptions={{ color, weight: 2, fillOpacity: 0.05 }} interactive={false} />
      )}
    </>
  );
}

function MapCommander({
  pendingWaypointFor,
  selectedVehicle,
  onWaypoint,
  onMapClick,
}: {
  pendingWaypointFor: string | null;
  selectedVehicle: Vehicle | null;
  onWaypoint: (vehicleId: string, lat: number, lon: number, altitude: number) => void;
  onMapClick?: (lat: number, lon: number) => void;
}) {
  useMapEvents({
    click(event) {
      if (onMapClick) {
        onMapClick(event.latlng.lat, event.latlng.lng);
        return;
      }
      if (!pendingWaypointFor) {
        return;
      }
      const altitude = selectedVehicle?.position?.altitude ?? 0;
      onWaypoint(pendingWaypointFor, event.latlng.lat, event.latlng.lng, altitude);
    },
  });
  return null;
}

function MapPanTracker({ onManualPan }: { onManualPan: () => void }) {
  useMapEvents({
    dragstart() {
      onManualPan();
    },
  });
  return null;
}

function FollowYpCenter({ yp, enabled }: { yp?: Vehicle; enabled: boolean }) {
  const map = useMap();
  const latitude = yp?.position?.latitude;
  const longitude = yp?.position?.longitude;

  useEffect(() => {
    if (!enabled || latitude == null || longitude == null) {
      return;
    }
    map.setView([latitude, longitude], map.getZoom(), { animate: false });
  }, [enabled, latitude, longitude, map]);

  return null;
}

function FitAllControl({ vehicles }: { vehicles: Vehicle[] }) {
  const map = useMap();

  return (
    <button
      className="fit-control"
      title="Fit all vehicles"
      onClick={() => {
        if (vehicles.length === 0) {
          return;
        }
        const bounds = L.latLngBounds(vehicles.map((vehicle) => [vehicle.position!.latitude, vehicle.position!.longitude]));
        map.fitBounds(bounds.pad(0.25), { maxZoom: 17 });
      }}
    >
      <LocateFixed size={18} />
    </button>
  );
}

function VehicleModal({ vehicle, onClose, onRtb, onWaypoint }: { vehicle: Vehicle; onClose: () => void; onRtb: () => void; onWaypoint: () => void }) {
  const position = vehicle.position;
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="vehicle-modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <div className={`type-chip ${vehicle.vehicle_type}`}>{vehicle.vehicle_type.toUpperCase()}</div>
          <div>
            <h2>{vehicle.vehicle_id}</h2>
            <p>{vehicle.connected ? "Connected" : "Last seen offline"}</p>
          </div>
          <button className="icon-button" title="Close" onClick={onClose}>
            <X size={20} />
          </button>
        </div>
        <div className="metrics">
          <Metric label="Latitude" value={position?.latitude.toFixed(6) ?? "--"} />
          <Metric label="Longitude" value={position?.longitude.toFixed(6) ?? "--"} />
          <Metric label="Altitude" value={`${(position?.altitude ?? 0).toFixed(1)} m`} />
          <Metric label="Heading" value={`${(vehicle.heading ?? 0).toFixed(0)} deg`} />
          <Metric label="Battery" value={vehicle.battery?.percentage == null ? "--" : `${Math.round(vehicle.battery.percentage * 100)}%`} />
        </div>
        <div className="modal-actions">
          <button className="danger" onClick={onRtb}>
            <RotateCcw size={18} />
            RTB
          </button>
          <button className="primary" onClick={onWaypoint}>
            <Route size={18} />
            Waypoint
          </button>
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function TelemetryTooltip({ vehicle }: { vehicle: Vehicle }) {
  return (
    <div className="tooltip-data">
      <strong>{vehicle.vehicle_id}</strong>
      <span>{vehicle.vehicle_type.toUpperCase()}</span>
      <span>Alt {(vehicle.position?.altitude ?? 0).toFixed(1)} m</span>
      <span>Hdg {(vehicle.heading ?? 0).toFixed(0)} deg</span>
      {vehicle.battery?.percentage != null && (
        <span className="battery-line">
          <Battery size={13} /> {Math.round(vehicle.battery.percentage * 100)}%
        </span>
      )}
    </div>
  );
}

function vehicleIcon(vehicle: Vehicle) {
  const type = vehicle.vehicle_type;
  const heading = vehicle.heading ?? 0;
  const altitude = vehicle.position?.altitude ?? 0;
  return L.divIcon({
    className: "",
    iconSize: [92, 50],
    iconAnchor: [46, 25],
    html: `
      <div class="marker-wrap">
        <div class="vehicle-marker ${type}" title="${vehicle.vehicle_id}" style="transform: rotate(${heading}deg)">
          ${vehicleGlyph(type)}
        </div>
        <div class="alt-label">${altitude.toFixed(0)} m</div>
      </div>
    `,
  });
}

function vehicleGlyph(type: VehicleType): string {
  if (type === "yp") {
    return `
      <svg viewBox="0 0 80 80" aria-hidden="true">
        <path class="yp-hull" d="M40 4 C55 18 62 43 55 70 C48 75 32 75 25 70 C18 43 25 18 40 4 Z" />
        <path class="yp-deck" d="M40 15 C49 28 53 47 50 64 C45 67 35 67 30 64 C27 47 31 28 40 15 Z" />
        <path class="yp-bridge" d="M31 31 H49 L47 48 H33 Z" />
        <path class="yp-window" d="M34 35 H39 V42 H34 Z" />
        <path class="yp-window" d="M42 35 H47 V42 H42 Z" />
        <path class="yp-mast" d="M40 31 V10 M40 18 H56 M40 18 L51 25" />
        <path class="yp-flag red" d="M56 15 L68 18 L56 22 Z" />
        <path class="yp-flag blue" d="M51 25 L63 28 L51 32 Z" />
        <path class="yp-rail" d="M25 57 H55" />
        <path class="yp-bow-line" d="M34 20 L40 9 L46 20" />
      </svg>
    `;
  }

  if (type === "uav") {
    return `
      <svg viewBox="0 0 64 64" aria-hidden="true">
        <path class="glyph-stroke" d="M32 32 L15 15 M32 32 L49 15 M32 32 L15 49 M32 32 L49 49" />
        <circle class="glyph-fill glyph-ring" cx="12" cy="12" r="8" />
        <circle class="glyph-fill glyph-ring" cx="52" cy="12" r="8" />
        <circle class="glyph-fill glyph-ring" cx="12" cy="52" r="8" />
        <circle class="glyph-fill glyph-ring" cx="52" cy="52" r="8" />
        <path class="glyph-fill" d="M32 9 L39 30 L32 37 L25 30 Z" />
        <circle class="glyph-fill" cx="32" cy="32" r="7" />
      </svg>
    `;
  }

  if (type === "uuv") {
    return `
      <svg viewBox="0 0 64 64" aria-hidden="true">
        <path class="glyph-fill" d="M32 5 C43 13 47 25 47 40 C47 53 41 59 32 59 C23 59 17 53 17 40 C17 25 21 13 32 5 Z" />
        <path class="glyph-cut" d="M24 36 H40" />
        <path class="glyph-fill glyph-fin" d="M17 40 L7 49 L18 50 Z" />
        <path class="glyph-fill glyph-fin" d="M47 40 L57 49 L46 50 Z" />
        <path class="glyph-fill glyph-fin" d="M27 54 L32 62 L37 54 Z" />
        <rect class="glyph-detail" x="27" y="18" width="10" height="8" rx="3" />
      </svg>
    `;
  }

  return `
    <svg viewBox="0 0 64 64" aria-hidden="true">
      <path class="glyph-fill" d="M32 4 C45 17 51 35 47 54 C41 58 23 58 17 54 C13 35 19 17 32 4 Z" />
      <path class="glyph-detail" d="M32 13 C39 23 42 35 40 49 C36 51 28 51 24 49 C22 35 25 23 32 13 Z" />
      <path class="glyph-cut" d="M20 43 H44" />
    </svg>
  `;
}

function vehicleColor(type: VehicleType): string {
  return {
    uav: "#f97316",
    usv: "#16a34a",
    uuv: "#eab308",
    yp: "#6b7280",
  }[type];
}
