import L from "leaflet";
import {
  AlertTriangle,
  Battery,
  Brush,
  Crosshair,
  EthernetPort,
  Grid3X3,
  Layers,
  LocateFixed,
  Maximize2,
  MessageSquare,
  RotateCcw,
  Route,
  Settings,
  Ship,
  Video,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { Circle, CircleMarker, MapContainer, Marker, Polyline, Popup, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";

import { fetchSettings, sendCommand, triggerMOB, updateSettings, websocketUrl } from "./api";
import type { Command, Vehicle, VehicleType } from "./types";

const USNA_CENTER: [number, number] = [38.9822, -76.4819];
const MAX_MESSAGE_LOG = 700;
const DEMO_MODE = import.meta.env.VITE_STATIC_DEMO === "true" || window.location.pathname.startsWith("/demo") || window.location.search.includes("demo=true");
const YP_DEMO_SPEED_MPS = 5 * 0.514444;
const YP_DEMO_HEADING = 330;
const DEMO_KEEP_IN_RANGE_M = 200;
const LOW_BATTERY_THRESHOLD = 0.25;
const BRAND_LOGO_URL = `${import.meta.env.BASE_URL}logos/usna_crest_jhublue.png`;
const USV_STREAM_URL = `${import.meta.env.BASE_URL}media/usv-stream.mp4`;

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

interface MapActionMenuState {
  lat: number;
  lon: number;
  x: number;
  y: number;
}

type DemoVehicleWithStyle = Vehicle & { marker_color?: string };

interface WaypointMarker {
  vehicle_id: string;
  latitude: number;
  longitude: number;
}

const VEHICLE_COLOR_PALETTE = [
  "#dc2626",
  "#ef4444",
  "#f97316",
  "#f59e0b",
  "#eab308",
  "#84cc16",
  "#16a34a",
  "#14b8a6",
  "#06b6d4",
  "#0ea5e9",
  "#2563eb",
  "#4f46e5",
  "#7c3aed",
  "#c026d3",
  "#db2777",
  "#6b7280",
];

export function App() {
  const isPhoneViewer = useIsPhoneViewer();
  const [vehicles, setVehicles] = useState<Record<string, Vehicle>>({});
  const [connected, setConnected] = useState(false);
  const [selected, setSelected] = useState<Vehicle | null>(null);
  const [trailSeconds, setTrailSeconds] = useState(45);
  const [showSettings, setShowSettings] = useState(false);
  const [showMessages, setShowMessages] = useState(false);
  const [messagePanelWidth, setMessagePanelWidth] = useState(500);
  const [topicFilters, setTopicFilters] = useState<string[]>([]);
  const [messageLog, setMessageLog] = useState<StreamMessage[]>([]);
  const [mapBase, setMapBase] = useState<MapBase>("satellite");
  const [mapSource, setMapSource] = useState<MapSource>(DEMO_MODE ? "online" : "auto");
  const [followYp, setFollowYp] = useState(true);
  const [showYpRangeRings, setShowYpRangeRings] = useState(true);
  const [messageRetentionMinutes, setMessageRetentionMinutes] = useState(10);
  const [settingsLoaded, setSettingsLoaded] = useState(DEMO_MODE);
  const [mapActionMenu, setMapActionMenu] = useState<MapActionMenuState | null>(null);
  const [streamVehicleId, setStreamVehicleId] = useState<string | null>(null);
  const [preferredWaypointVehicleId, setPreferredWaypointVehicleId] = useState<string | null>(null);
  const [waypointMarkers, setWaypointMarkers] = useState<Record<string, WaypointMarker>>({});
  const [mobModalOpen, setMobModalOpen] = useState(false);
  const [mobSending, setMobSending] = useState(false);
  const followBeforeWaypointDragRef = useRef(false);
  const wsRef = useRef<WebSocket | null>(null);
  const demoSimsRef = useRef<DemoVehicle[]>([]);
  const localVehicleColorsRef = useRef<Record<string, string>>({});

  useEffect(() => {
    if (!DEMO_MODE || !("serviceWorker" in navigator)) {
      return;
    }
    navigator.serviceWorker.register(`${import.meta.env.BASE_URL}tile-cache-sw.js`).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (DEMO_MODE) {
      return;
    }
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
          const snapshotVehicles = payload.vehicles as Vehicle[];
          setVehicles(Object.fromEntries(snapshotVehicles.map((vehicle) => [vehicle.vehicle_id, withLocalVehicleColor(vehicle, localVehicleColorsRef.current)])));
          setMessageLog(snapshotMessages(snapshotVehicles).slice(0, MAX_MESSAGE_LOG));
        }
        if (payload.op === "vehicle_update") {
          const vehicle = withLocalVehicleColor(payload.vehicle, localVehicleColorsRef.current);
          setVehicles((current) => ({ ...current, [vehicle.vehicle_id]: vehicle }));
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
      };
    };

    connect();
    return () => {
      window.clearTimeout(retry);
      wsRef.current?.close();
    };
  }, []);

  useEffect(() => {
    if (!DEMO_MODE) {
      return;
    }
    demoSimsRef.current = createDemoVehicles();
    setWaypointMarkers(
      Object.fromEntries(
        demoSimsRef.current
          .filter((vehicle) => vehicle.vehicle_type !== "yp")
          .map((vehicle) => [vehicle.vehicle_id, { vehicle_id: vehicle.vehicle_id, latitude: vehicle.target.latitude, longitude: vehicle.target.longitude }]),
      ),
    );
    let lastStep = Date.now() / 1000;
    const tick = () => {
      const now = Date.now() / 1000;
      const dt = Math.max(0.001, now - lastStep);
      lastStep = now;
      const messages = demoSimsRef.current.flatMap((vehicle) => stepDemoVehicle(vehicle, dt, now, demoSimsRef.current));
      const demoVehicles = demoSimsRef.current.map(demoVehicleSnapshot);
      setConnected(true);
      setVehicles(Object.fromEntries(demoVehicles.map((vehicle) => [vehicle.vehicle_id, vehicle])));
      setMessageLog((current) => [...messages.map(streamMessageFromPayload).reverse(), ...current].slice(0, MAX_MESSAGE_LOG));
    };
    tick();
    const interval = window.setInterval(tick, 200);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    if (DEMO_MODE) {
      return;
    }
    let cancelled = false;
    fetchSettings()
      .then((serverSettings) => {
        if (cancelled) {
          return;
        }
        setMessageRetentionMinutes(Math.round(serverSettings.message_retention_seconds / 60));
        setSettingsLoaded(true);
      })
      .catch(() => setSettingsLoaded(true));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (DEMO_MODE || !settingsLoaded) {
      return;
    }
    const timeout = window.setTimeout(() => {
      updateSettings({ message_retention_seconds: messageRetentionMinutes * 60 }).catch(() => undefined);
    }, 350);
    return () => window.clearTimeout(timeout);
  }, [messageRetentionMinutes, settingsLoaded]);

  const vehicleList = useMemo(() => Object.values(vehicles).filter((vehicle) => vehicle.position), [vehicles]);
  const yp = vehicleList.find((vehicle) => vehicle.vehicle_type === "yp");
  const ypGpsLinked = Boolean(yp?.connected);
  const center: [number, number] = yp?.position ? [yp.position.latitude, yp.position.longitude] : USNA_CENTER;
  const filteredMessages = useMemo(() => filterMessages(messageLog, topicFilters), [messageLog, topicFilters]);
  const renderedMapSource = DEMO_MODE ? "online" : mapSource;
  const mapLayer = useMemo(() => tileLayerFor(mapBase, renderedMapSource), [mapBase, renderedMapSource]);

  const command = (vehicleId: string, body: Command) => {
    if (DEMO_MODE) {
      handleDemoCommand(demoSimsRef.current, vehicleId, body);
      return;
    }
    sendCommand(wsRef.current, vehicleId, body);
  };

  const sendWaypoint = (vehicleId: string, lat: number, lon: number, altitude?: number) => {
    command(vehicleId, { type: "waypoint", target: { latitude: lat, longitude: lon, altitude: altitude ?? vehicles[vehicleId]?.position?.altitude ?? 0 } });
    setWaypointMarkers((current) => ({
      ...current,
      [vehicleId]: { vehicle_id: vehicleId, latitude: lat, longitude: lon },
    }));
  };

  const sendSearchGrid = (vehicleId: string, lat: number, lon: number, gridSizeM: number, swathM: number, altM: number) => {
    command(vehicleId, { type: "search_grid", lat, lon, grid_size_m: gridSizeM, swath_m: swathM, altitude_m: altM });
    setMapActionMenu(null);
  };

  const handleMobConfirm = async () => {
  setMobSending(true);
  try {
    const result = await triggerMOB();
    const vehicleId = result.vehicle_id ?? "unknown";

    const mobMessage: StreamMessage = {
      id: `mob-${Date.now()}`,
      receivedAt: Date.now(),
      vehicle_id: vehicleId,
      vehicle_type: "uav",
      topic: `/vehicles/${vehicleId}/commands`,
      type: "yp_ground_station/MOBTriggered",
      stamp: Date.now() / 1000,
      msg: result.ok
        ? { status: "dispatched", vehicle_id: vehicleId }
        : { status: "failed", error: result.error },
    };

    setMessageLog((current) => [mobMessage, ...current].slice(0, MAX_MESSAGE_LOG));
  } catch {
    // swallow — error already logged via message log above
  }
  setMobSending(false);
  setMobModalOpen(false);
};

  const sendAllToMapPoint = (lat: number, lon: number) => {
    const commandableVehicles = vehicleList.filter((candidate) => candidate.vehicle_type !== "yp");
    const nextMarkers: Record<string, WaypointMarker> = {};
    commandableVehicles.forEach((vehicle, index) => {
      const offset = waypointOffset(lat, lon, index, commandableVehicles.length);
      command(vehicle.vehicle_id, { type: "waypoint", target: { latitude: offset.latitude, longitude: offset.longitude, altitude: vehicle.position?.altitude ?? 0 } });
      nextMarkers[vehicle.vehicle_id] = { vehicle_id: vehicle.vehicle_id, latitude: offset.latitude, longitude: offset.longitude };
    });
    if (commandableVehicles.length > 0) {
      setWaypointMarkers((current) => ({ ...current, ...nextMarkers }));
    }
  };

  const setVehicleColor = (vehicleId: string, color: string) => {
    localVehicleColorsRef.current = {
      ...localVehicleColorsRef.current,
      [vehicleId]: color,
    };
    updateDemoVehicleColor(demoSimsRef.current, vehicleId, color);
    setVehicles((current) => ({
      ...current,
      [vehicleId]: {
        ...current[vehicleId],
        marker_color: color,
      } as DemoVehicleWithStyle,
    }));
    setSelected((current) => (current?.vehicle_id === vehicleId ? ({ ...current, marker_color: color } as DemoVehicleWithStyle) : current));
  };

  return (
    <div className="app" onClick={() => mapActionMenu && setMapActionMenu(null)}>
      <MapContainer center={center} zoom={17} minZoom={3} maxZoom={19} zoomControl className="map">
        <TileLayer key={`${mapBase}-${renderedMapSource}`} url={mapLayer.url} attribution={mapLayer.attribution} />
        <MapCommander
          onMapAction={(lat, lon, point) => setMapActionMenu({ lat, lon, x: point.x, y: point.y })}
        />
        <MapPanTracker onManualPan={() => setFollowYp(false)} />
        <FollowYpCenter yp={yp} enabled={followYp} />
        <FitAllControl vehicles={vehicleList} />
        {showYpRangeRings && <YpRangeRings yp={yp} />}
        {Object.values(waypointMarkers).map((waypoint) => (
          <WaypointCrosshair
            key={waypoint.vehicle_id}
            waypoint={waypoint}
            vehicle={vehicles[waypoint.vehicle_id]}
            onDragStart={() => {
              followBeforeWaypointDragRef.current = followYp;
              setFollowYp(false);
            }}
            onMove={(lat, lon) => sendWaypoint(waypoint.vehicle_id, lat, lon)}
            onDragEnd={() => {
              if (followBeforeWaypointDragRef.current) {
                window.setTimeout(() => setFollowYp(true), 250);
              }
            }}
          />
        ))}
        {vehicleList.map((vehicle) => (
          <VehicleLayer
            key={vehicle.vehicle_id}
            vehicle={vehicle}
            trailSeconds={trailSeconds}
            isPhoneViewer={isPhoneViewer}
            onClick={() => {
              setMapActionMenu(null);
              if (vehicle.vehicle_type === "yp") {
                setFollowYp(true);
              }
              setSelected(vehicle);
            }}
          />
        ))}
      </MapContainer>

      {mapActionMenu && (
        <MapActionMenu
          menu={mapActionMenu}
          vehicles={vehicleList}
          preferredVehicleId={preferredWaypointVehicleId}
          onSend={(vehicleId) => {
            sendWaypoint(vehicleId, mapActionMenu.lat, mapActionMenu.lon);
            setPreferredWaypointVehicleId(null);
            setMapActionMenu(null);
          }}
          onSendAll={() => {
            sendAllToMapPoint(mapActionMenu.lat, mapActionMenu.lon);
            setPreferredWaypointVehicleId(null);
            setMapActionMenu(null);
          }}
          onSearchGrid={(vehicleId, gridSizeM, swathM, altM) =>
            sendSearchGrid(vehicleId, mapActionMenu.lat, mapActionMenu.lon, gridSizeM, swathM, altM)
          }
        />
      )}

      <MapMenu mapBase={mapBase} mapSource={mapSource} onMapBaseChange={setMapBase} onMapSourceChange={setMapSource} />

      <div className="topbar">
        <div className="brand">
          <img className="brand-logo" src={BRAND_LOGO_URL} alt="USNA crest" />
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
          <label className="setting-toggle">
            <span>YP range rings</span>
            <input type="checkbox" checked={showYpRangeRings} onChange={(event) => setShowYpRangeRings(event.target.checked)} />
          </label>
          <label>
            DB retention
            <span>{messageRetentionMinutes} min</span>
          </label>
          <input
            min={1}
            max={1440}
            step={1}
            type="range"
            value={messageRetentionMinutes}
            disabled={DEMO_MODE}
            onChange={(event) => setMessageRetentionMinutes(Number(event.target.value))}
          />
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
            setPreferredWaypointVehicleId(selected.vehicle_id);
            setSelected(null);
          }}
          onStreamVideo={() => {
            setStreamVehicleId(selected.vehicle_id);
            setSelected(null);
          }}
          onColorSave={(color) => setVehicleColor(selected.vehicle_id, color)}
        />
      )}

      {streamVehicleId && (
        <UsvVideoViewer
          vehicleId={streamVehicleId}
          src={USV_STREAM_URL}
          onClose={() => setStreamVehicleId(null)}
        />
      )}

      {/* Fixed red MOB button — always visible, bottom-right corner */}
      <button
        className="mob-button"
        title="Man Overboard — dispatch SAR search"
        onClick={(e) => {
          e.stopPropagation();
          setMobModalOpen(true);
        }}
      >
        MAN<br />OVER<br />BOARD
      </button>

      {/* MOB confirmation modal */}
      {mobModalOpen && (
        <div className="mob-modal-overlay" onClick={() => !mobSending && setMobModalOpen(false)}>
          <div className="mob-modal" onClick={(e) => e.stopPropagation()}>
            <div className="mob-modal-title">
              <AlertTriangle size={22} />
              Man Overboard
            </div>
            <div className="mob-modal-body">
              This will immediately dispatch the nearest available vehicle to search
              the YP vessel&apos;s recent track. Confirm only if a person is overboard.
            </div>
            <div className="mob-modal-actions">
              <button
                className="mob-cancel-btn"
                onClick={() => setMobModalOpen(false)}
                disabled={mobSending}
              >
                Cancel
              </button>
              <button
                className="mob-confirm-btn"
                onClick={handleMobConfirm}
                disabled={mobSending}
              >
                {mobSending ? "Dispatching…" : "MAN OVERBOARD!"}
              </button>
            </div>
          </div>
        </div>
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

function snapshotMessages(vehicles: Vehicle[]): StreamMessage[] {
  const receivedAt = Date.now();
  return vehicles.flatMap((vehicle) =>
    Object.entries(vehicle.messages ?? {}).map(([topic, message]) => ({
      id: `${topic}-${message.stamp}-snapshot-${receivedAt}`,
      receivedAt,
      vehicle_id: vehicle.vehicle_id,
      vehicle_type: vehicle.vehicle_type,
      topic,
      type: message.type,
      stamp: message.stamp,
      msg: message.msg,
    })),
  ).sort((a, b) => b.stamp - a.stamp);
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

function useIsPhoneViewer(): boolean {
  const query = "(pointer: coarse)";
  const getMatches = () => (typeof window === "undefined" ? false : isPhoneBrowser() && window.matchMedia(query).matches);
  const [isPhoneViewer, setIsPhoneViewer] = useState(getMatches);

  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setIsPhoneViewer(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return isPhoneViewer;
}

function isPhoneBrowser(): boolean {
  if (typeof navigator === "undefined") {
    return false;
  }
  const userAgentData = (navigator as Navigator & { userAgentData?: { mobile?: boolean } }).userAgentData;
  if (typeof userAgentData?.mobile === "boolean") {
    return userAgentData.mobile;
  }
  return /iPhone|iPod|Android.*Mobile|Windows Phone|Mobi/i.test(navigator.userAgent);
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
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="map-menu-shell" aria-label="Map options">
      <button className="map-menu-toggle" title="Map layers" onClick={() => setExpanded((value) => !value)}>
        <Layers size={19} />
      </button>
      {expanded && (
        <div className="map-menu">
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
      )}
    </div>
  );
}

function MapActionMenu({
  menu,
  vehicles,
  preferredVehicleId,
  onSend,
  onSendAll,
  onSearchGrid,
}: {
  menu: MapActionMenuState;
  vehicles: Vehicle[];
  preferredVehicleId: string | null;
  onSend: (vehicleId: string) => void;
  onSendAll: () => void;
  onSearchGrid: (vehicleId: string, gridSizeM: number, swathM: number, altM: number) => void;
}) {
  const [showVehicles, setShowVehicles] = useState(false);
  const [showSearchGrid, setShowSearchGrid] = useState(false);
  const [gridSizeM, setGridSizeM] = useState(200);
  const [swathM, setSwathM] = useState(20);
  const [altM, setAltM] = useState(30);
  const commandableVehicles = vehicles.filter((vehicle) => vehicle.vehicle_type !== "yp");
  const preferredVehicle = commandableVehicles.find((vehicle) => vehicle.vehicle_id === preferredVehicleId);
  return (
    <div className="map-action-menu" style={{ left: menu.x, top: menu.y }} onClick={(event) => event.stopPropagation()}>
      <div className="map-action-title">Waypoint</div>
      <div className="map-coordinates">
        <span>Lat {menu.lat.toFixed(6)}</span>
        <span>Lon {menu.lon.toFixed(6)}</span>
      </div>
      {preferredVehicle && (
        <button className="map-action-preferred" onClick={() => onSend(preferredVehicle.vehicle_id)}>
          <span className={`vehicle-dot ${preferredVehicle.vehicle_type}`} style={{ backgroundColor: vehicleMarkerColor(preferredVehicle) }} />
          Send {preferredVehicle.vehicle_id}
        </button>
      )}
      <button onClick={() => setShowVehicles((value) => !value)}>
        <Route size={15} />
        Send vehicle
      </button>
      {showVehicles &&
        commandableVehicles.map((vehicle) => (
          <button key={vehicle.vehicle_id} className="map-action-child" onClick={() => onSend(vehicle.vehicle_id)}>
            <span className={`vehicle-dot ${vehicle.vehicle_type}`} style={{ backgroundColor: vehicleMarkerColor(vehicle) }} />
            {vehicle.vehicle_id}
          </button>
        ))}
      {commandableVehicles.length > 1 && (
        <button className="map-action-all" onClick={onSendAll}>
          All vehicles
        </button>
      )}

      {/* Search Grid section */}
      <div className="map-action-section">
        <button onClick={() => setShowSearchGrid((v) => !v)}>
          <Grid3X3 size={15} />
          Search Grid Here
        </button>
        {showSearchGrid && commandableVehicles.length > 0 && (
          <div className="search-grid-form">
            <label>
              Grid size
              <span>{gridSizeM} m</span>
            </label>
            <input
              type="range" min={50} max={500} step={25}
              value={gridSizeM}
              onChange={(e) => setGridSizeM(Number(e.target.value))}
            />
            <label>
              Swath width
              <span>{swathM} m</span>
            </label>
            <input
              type="range" min={5} max={50} step={5}
              value={swathM}
              onChange={(e) => setSwathM(Number(e.target.value))}
            />
            <label>
              Altitude
              <span>{altM} m</span>
            </label>
            <input
              type="range" min={10} max={100} step={5}
              value={altM}
              onChange={(e) => setAltM(Number(e.target.value))}
            />
            {commandableVehicles.map((vehicle) => (
              <button
                key={vehicle.vehicle_id}
                className="search-grid-launch-btn"
                onClick={() => onSearchGrid(vehicle.vehicle_id, gridSizeM, swathM, altM)}
              >
                <span className={`vehicle-dot ${vehicle.vehicle_type}`} style={{ backgroundColor: vehicleMarkerColor(vehicle) }} />
                Launch on {vehicle.vehicle_id}
              </button>
            ))}
          </div>
        )}
        {showSearchGrid && commandableVehicles.length === 0 && (
          <div className="search-grid-form">
            <span style={{ color: "#94a3b8", fontSize: 12 }}>No commandable vehicles connected</span>
          </div>
        )}
      </div>
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
  const messageListRef = useRef<HTMLDivElement | null>(null);
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

  const toggleMessage = (message: StreamMessage) => {
    setSelectedMessage((current) => {
      if (current?.id === message.id) {
        window.setTimeout(() => messageListRef.current?.scrollTo({ top: 0, behavior: "smooth" }), 0);
        return null;
      }
      return message;
    });
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

      <div className="message-list" ref={messageListRef}>
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
            <pre className="json-view">{jsonSyntaxHighlight(selectedMessage.msg)}</pre>
          </article>
        )}
        {filteredMessages.length === 0 ? (
          <div className="empty-messages">No messages match the current filter.</div>
        ) : (
          filteredMessages.map((message) => (
            <button
              key={message.id}
              className={selectedMessage?.id === message.id ? "message-row selected" : "message-row"}
              onClick={() => toggleMessage(message)}
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

function jsonSyntaxHighlight(value: unknown): ReactNode {
  const json = JSON.stringify(value, null, 2);
  const parts = json.split(/("(?:\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\btrue\b|\bfalse\b|\bnull\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g);
  return parts.map((part, index) => {
    if (!part) {
      return null;
    }
    let className = "json-punctuation";
    if (/^"/.test(part)) {
      className = /:$/.test(part) ? "json-key" : "json-string";
    } else if (/true|false/.test(part)) {
      className = "json-boolean";
    } else if (/null/.test(part)) {
      className = "json-null";
    } else if (/^-?\d/.test(part)) {
      className = "json-number";
    }
    return (
      <span key={`${part}-${index}`} className={className}>
        {part}
      </span>
    );
  });
}

function VehicleLayer({
  vehicle,
  trailSeconds,
  isPhoneViewer,
  onClick,
}: {
  vehicle: Vehicle;
  trailSeconds: number;
  isPhoneViewer: boolean;
  onClick: () => void;
}) {
  const position = vehicle.position!;
  const cutoff = Date.now() / 1000 - trailSeconds;
  const trail = (vehicle.history ?? [])
    .filter((point) => !point.stamp || point.stamp >= cutoff)
    .map((point) => [point.latitude, point.longitude] as [number, number]);
  const color = vehicleMarkerColor(vehicle);

  return (
    <>
      {trail.length > 1 && <Polyline positions={trail} pathOptions={{ color, weight: 3, opacity: 0.75 }} />}
      <Marker
        position={[position.latitude, position.longitude]}
        icon={vehicleIcon(vehicle, isPhoneViewer)}
        zIndexOffset={vehicleZIndexOffset(vehicle.vehicle_type)}
        eventHandlers={{
          click: (event) => {
            L.DomEvent.stopPropagation(event.originalEvent);
            onClick();
          },
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
  onMapAction,
}: {
  onMapAction: (lat: number, lon: number, point: L.Point) => void;
}) {
  const map = useMap();
  useMapEvents({
    contextmenu(event) {
      const point = map.latLngToContainerPoint(event.latlng);
      onMapAction(event.latlng.lat, event.latlng.lng, point);
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

function YpRangeRings({ yp }: { yp?: Vehicle }) {
  const position = yp?.position;
  if (!position) {
    return null;
  }

  return (
    <>
      {[50, 100, 200].map((radius) => (
        <Circle
          key={radius}
          center={[position.latitude, position.longitude]}
          radius={radius}
          pathOptions={{
            color: "#38bdf8",
            dashArray: radius === 200 ? "6 8" : undefined,
            fillColor: "#38bdf8",
            fillOpacity: 0.035,
            opacity: 0.6,
            weight: 1.5,
          }}
          interactive={false}
        />
      ))}
    </>
  );
}

function WaypointCrosshair({
  waypoint,
  vehicle,
  onDragStart,
  onDragEnd,
  onMove,
}: {
  waypoint: WaypointMarker;
  vehicle?: Vehicle;
  onDragStart: () => void;
  onDragEnd: () => void;
  onMove: (lat: number, lon: number) => void;
}) {
  const color = vehicle ? vehicleMarkerColor(vehicle) : "#0f172a";
  const position = useMemo<[number, number]>(() => [waypoint.latitude, waypoint.longitude], [waypoint.latitude, waypoint.longitude]);
  const icon = useMemo(() => waypointIcon(color), [color]);
  return (
    <Marker
      position={position}
      icon={icon}
      zIndexOffset={6000}
      draggable
      eventHandlers={{
        click: (event) => L.DomEvent.stopPropagation(event.originalEvent),
        dragstart: onDragStart,
        dragend: (event) => {
          const position = event.target.getLatLng();
          onMove(position.lat, position.lng);
          onDragEnd();
        },
      }}
    />
  );
}

function waypointIcon(color: string) {
  return L.divIcon({
    className: "",
    iconSize: [44, 44],
    iconAnchor: [22, 22],
    html: `
      <div class="waypoint-crosshair" style="--waypoint-color: ${color}">
        <svg viewBox="0 0 34 34" aria-hidden="true">
          <circle cx="17" cy="17" r="7" />
          <path d="M17 2 V11 M17 23 V32 M2 17 H11 M23 17 H32" />
        </svg>
      </div>
    `,
  });
}

function VehicleModal({
  vehicle,
  onClose,
  onRtb,
  onWaypoint,
  onStreamVideo,
  onColorSave,
}: {
  vehicle: Vehicle;
  onClose: () => void;
  onRtb: () => void;
  onWaypoint: () => void;
  onStreamVideo: () => void;
  onColorSave: (color: string) => void;
}) {
  const position = vehicle.position;
  const [showColorPalette, setShowColorPalette] = useState(false);
  const [draftColor, setDraftColor] = useState(vehicleMarkerColor(vehicle));
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
          <button className="secondary" onClick={() => setShowColorPalette((value) => !value)}>
            <Brush size={18} />
            Color
          </button>
          {vehicle.vehicle_type === "usv" && (
            <button className="stream" onClick={onStreamVideo}>
              <Video size={18} />
              Stream Video
            </button>
          )}
          <button className="primary" onClick={onWaypoint}>
            <Route size={18} />
            Waypoint
          </button>
        </div>
        {showColorPalette && (
          <div className="color-panel">
            <div className="color-swatches">
              {VEHICLE_COLOR_PALETTE.map((color) => (
                <button
                  key={color}
                  className={draftColor === color ? "color-swatch selected" : "color-swatch"}
                  style={{ backgroundColor: color }}
                  title={color}
                  onClick={() => {
                    setDraftColor(color);
                    onColorSave(color);
                  }}
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function UsvVideoViewer({
  vehicleId,
  src,
  onClose,
}: {
  vehicleId: string;
  src: string;
  onClose: () => void;
}) {
  const [frame, setFrame] = useState(() => ({
    x: Math.max(16, window.innerWidth - 456),
    y: 120,
    width: Math.min(420, window.innerWidth - 32),
    height: 320,
  }));
  const dragRef = useRef<{
    mode: "move" | "resize";
    pointerId: number;
    startX: number;
    startY: number;
    frame: typeof frame;
  } | null>(null);

  const updateFrame = (next: typeof frame) => {
    const maxWidth = Math.max(280, window.innerWidth - 24);
    const maxHeight = Math.max(220, window.innerHeight - 24);
    const width = Math.min(maxWidth, Math.max(280, next.width));
    const height = Math.min(maxHeight, Math.max(220, next.height));
    setFrame({
      width,
      height,
      x: Math.min(Math.max(12, next.x), Math.max(12, window.innerWidth - width - 12)),
      y: Math.min(Math.max(12, next.y), Math.max(12, window.innerHeight - height - 12)),
    });
  };

  const startDrag = (mode: "move" | "resize", event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      mode,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      frame,
    };
  };

  const moveDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) {
      return;
    }
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (drag.mode === "move") {
      updateFrame({ ...drag.frame, x: drag.frame.x + dx, y: drag.frame.y + dy });
      return;
    }
    updateFrame({ ...drag.frame, width: drag.frame.width + dx, height: drag.frame.height + dy });
  };

  const endDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) {
      dragRef.current = null;
    }
  };

  return (
    <section
      className="video-viewer"
      style={{ left: frame.x, top: frame.y, width: frame.width, height: frame.height }}
      onMouseDown={(event) => event.stopPropagation()}
    >
      <header className="video-viewer-header" onPointerDown={(event) => startDrag("move", event)} onPointerMove={moveDrag} onPointerUp={endDrag}>
        <div>
          <Video size={16} />
          <strong>{vehicleId}</strong>
        </div>
        <button className="icon-button" title="Close stream" onPointerDown={(event) => event.stopPropagation()} onClick={onClose}>
          <X size={17} />
        </button>
      </header>
      <video className="video-viewer-media" src={src} autoPlay muted loop playsInline controls />
      <button
        className="video-resize-handle"
        title="Resize stream"
        onPointerDown={(event) => startDrag("resize", event)}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
      >
        <Maximize2 size={15} />
      </button>
    </section>
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

interface DemoVehicle {
  vehicle_id: string;
  vehicle_type: VehicleType;
  lat: number;
  lon: number;
  alt: number;
  heading: number;
  battery: number;
  speed: number;
  batteryDrainPerSecond: number;
  marker_color: string;
  manualWaypoint: boolean;
  target: { latitude: number; longitude: number; altitude: number };
  mode: string;
  history: Vehicle["history"];
  messages: Vehicle["messages"];
  localX: number;
  localY: number;
}

function createDemoVehicles(): DemoVehicle[] {
  const base = { latitude: 38.984764, longitude: -76.478643 };
  const vehicles = [
    createDemoVehicle("yp", "yp", base.latitude, base.longitude, 2, YP_DEMO_HEADING, YP_DEMO_SPEED_MPS, 0.000002),
    createDemoVehicle("demo-uav-1", "uav", base.latitude + 0.00072, base.longitude - 0.00058, 48, 122, 9, 0.0018, 0.82),
    createDemoVehicle("demo-uav-2", "uav", base.latitude + 0.00042, base.longitude + 0.00075, 42, 210, 8, 0.0032, 0.32),
    createDemoVehicle("demo-usv-1", "usv", base.latitude - 0.00048, base.longitude + 0.00046, 0, 40, 2.8, 0.0011, 0.76),
    createDemoVehicle("demo-usv-2", "usv", base.latitude - 0.00078, base.longitude - 0.00008, 0, 275, 2.5, 0.0024, 0.44),
    createDemoVehicle("demo-uuv-1", "uuv", base.latitude - 0.00064, base.longitude - 0.00042, -8, 255, 1.3, 0.0015, 0.68),
  ];
  const typeCounts: Partial<Record<VehicleType, number>> = {};
  for (const vehicle of vehicles) {
    const typeIndex = typeCounts[vehicle.vehicle_type] ?? 0;
    vehicle.marker_color = assignedVehicleColor(vehicle.vehicle_type, typeIndex);
    typeCounts[vehicle.vehicle_type] = typeIndex + 1;
  }
  seedForwardDemoWaypoints(vehicles);
  return vehicles;
}

function createDemoVehicle(
  vehicle_id: string,
  vehicle_type: VehicleType,
  lat: number,
  lon: number,
  alt: number,
  heading: number,
  speed: number,
  batteryDrainPerSecond: number,
  battery = 0.86,
): DemoVehicle {
  return {
    vehicle_id,
    vehicle_type,
    lat,
    lon,
    alt,
    heading,
    speed,
    battery: vehicle_type === "yp" ? 1 : battery,
    batteryDrainPerSecond,
    marker_color: vehicleColor(vehicle_type),
    manualWaypoint: false,
    target: randomDemoTarget(lat, lon, alt),
    mode: "loiter",
    history: [],
    messages: {},
    localX: 0,
    localY: 0,
  };
}

function stepDemoVehicle(vehicle: DemoVehicle, dt: number, stamp: number, vehicles: DemoVehicle[]): Array<Parameters<typeof streamMessageFromPayload>[0]> {
  if (vehicle.vehicle_type === "yp") {
    vehicle.heading = YP_DEMO_HEADING;
    const next = destinationPoint(vehicle.lat, vehicle.lon, vehicle.heading, YP_DEMO_SPEED_MPS * dt);
    vehicle.lat = next.latitude;
    vehicle.lon = next.longitude;
    vehicle.localX += Math.sin((vehicle.heading * Math.PI) / 180) * YP_DEMO_SPEED_MPS * dt;
    vehicle.localY += Math.cos((vehicle.heading * Math.PI) / 180) * YP_DEMO_SPEED_MPS * dt;
    vehicle.history = [...(vehicle.history ?? []), { stamp, latitude: vehicle.lat, longitude: vehicle.lon, altitude: vehicle.alt }].slice(-500);
    return recordDemoMessages(vehicle, stamp);
  }

  const yp = vehicles.find((candidate) => candidate.vehicle_type === "yp");
  if (vehicle.mode === "rtb" && yp) {
    vehicle.target = sternTargetForYp(yp, vehicle);
  } else if (!vehicle.manualWaypoint && yp) {
    const rangeFromYp = haversineMeters(vehicle.lat, vehicle.lon, yp.lat, yp.lon);
    const targetRange = haversineMeters(vehicle.target.latitude, vehicle.target.longitude, yp.lat, yp.lon);
    if (rangeFromYp > DEMO_KEEP_IN_RANGE_M || targetRange > DEMO_KEEP_IN_RANGE_M) {
      vehicle.target = randomDemoTargetNearYp(yp, vehicle);
    }
  }

  const distance = haversineMeters(vehicle.lat, vehicle.lon, vehicle.target.latitude, vehicle.target.longitude);
  if (distance < Math.max(3, vehicle.speed * dt * 2)) {
    if (vehicle.mode === "rtb") {
      vehicle.target = yp ? sternTargetForYp(yp, vehicle) : vehicle.target;
    } else if (vehicle.manualWaypoint) {
      vehicle.mode = "hold";
    } else {
      vehicle.target = yp ? randomDemoTargetNearYp(yp, vehicle) : randomDemoTarget(vehicle.lat, vehicle.lon, vehicle.alt);
    }
  } else {
    const bearing = bearingDegrees(vehicle.lat, vehicle.lon, vehicle.target.latitude, vehicle.target.longitude);
    vehicle.heading = smoothDegrees(vehicle.heading, bearing, Math.min(1, dt * 1.6));
    const travel = Math.min(distance, vehicle.speed * dt);
    const next = destinationPoint(vehicle.lat, vehicle.lon, vehicle.heading, travel);
    vehicle.lat = next.latitude;
    vehicle.lon = next.longitude;
    vehicle.alt += Math.max(-1, Math.min(1, vehicle.target.altitude - vehicle.alt)) * Math.min(1, dt);
    if (vehicle.vehicle_type === "usv") {
      vehicle.alt = 0;
    }
    if (vehicle.vehicle_type === "uuv") {
      vehicle.alt = Math.min(-1, vehicle.alt);
    }
    vehicle.localX += Math.sin((vehicle.heading * Math.PI) / 180) * travel;
    vehicle.localY += Math.cos((vehicle.heading * Math.PI) / 180) * travel;
  }
  vehicle.battery = Math.max(0.05, vehicle.battery - dt * vehicle.batteryDrainPerSecond);
  vehicle.history = [...(vehicle.history ?? []), { stamp, latitude: vehicle.lat, longitude: vehicle.lon, altitude: vehicle.alt }].slice(-500);
  return recordDemoMessages(vehicle, stamp);
}

function recordDemoMessages(vehicle: DemoVehicle, stamp: number): Array<Parameters<typeof streamMessageFromPayload>[0]> {
  const messages = demoMessages(vehicle, stamp);
  for (const message of messages) {
    vehicle.messages[message.topic ?? ""] = { type: message.type ?? "unknown", stamp, msg: message.msg ?? {} };
  }
  return messages;
}

function demoVehicleSnapshot(vehicle: DemoVehicle): Vehicle {
  return {
    vehicle_id: vehicle.vehicle_id,
    vehicle_type: vehicle.vehicle_type,
    connected: true,
    last_seen: Date.now() / 1000,
    last_seen_age: 0,
    position: { latitude: vehicle.lat, longitude: vehicle.lon, altitude: vehicle.alt },
    history: vehicle.history,
    heading: vehicle.heading,
    battery: { percentage: vehicle.battery, voltage: 22.2 * vehicle.battery, current: -4 },
    messages: vehicle.messages,
    marker_color: vehicle.marker_color,
  } as DemoVehicleWithStyle;
}

function demoMessages(vehicle: DemoVehicle, stamp: number): Array<Parameters<typeof streamMessageFromPayload>[0]> {
  const topic = (suffix: string) => `/vehicles/${vehicle.vehicle_id}/${suffix}`;
  const quat = yawToQuaternion(vehicle.heading);
  return [
    wrapDemoMessage(vehicle, topic("heartbeat"), "yp_ground_station/msg/Heartbeat", stamp, { mode: vehicle.mode, armed: true }),
    wrapDemoMessage(vehicle, topic("navsatfix"), "sensor_msgs/msg/NavSatFix", stamp, {
      latitude: vehicle.lat,
      longitude: vehicle.lon,
      altitude: vehicle.alt,
      heading: vehicle.heading,
      status: { status: 0, service: 1 },
    }),
    wrapDemoMessage(vehicle, topic("pose"), "geometry_msgs/msg/Pose", stamp, {
      position: { x: vehicle.localX, y: vehicle.localY, z: vehicle.alt },
      orientation: quat,
      heading: vehicle.heading,
    }),
    wrapDemoMessage(vehicle, topic("battery"), "sensor_msgs/msg/BatteryState", stamp, {
      voltage: 22.2 * vehicle.battery,
      current: -4,
      percentage: vehicle.battery,
      present: true,
    }),
    wrapDemoMessage(vehicle, topic("trajectory"), "trajectory_msgs/msg/MultiDOFJointTrajectory", stamp, {
      points: [{ transforms: [{ translation: { x: vehicle.localX, y: vehicle.localY, z: vehicle.alt }, rotation: quat }] }],
    }),
  ];
}

function wrapDemoMessage(vehicle: DemoVehicle, topic: string, type: string, stamp: number, msg: Record<string, unknown>): Parameters<typeof streamMessageFromPayload>[0] {
  return { vehicle_id: vehicle.vehicle_id, vehicle_type: vehicle.vehicle_type, topic, type, stamp, msg };
}

function handleDemoCommand(vehicles: DemoVehicle[], vehicleId: string, command: Command): void {
  const vehicle = vehicles.find((candidate) => candidate.vehicle_id === vehicleId);
  if (!vehicle) {
    return;
  }
  if (command.type === "rtb") {
    vehicle.mode = "rtb";
    vehicle.manualWaypoint = false;
    const yp = vehicles.find((candidate) => candidate.vehicle_type === "yp");
    vehicle.target = yp ? sternTargetForYp(yp, vehicle) : { latitude: 38.984764, longitude: -76.478643, altitude: vehicle.vehicle_type === "uuv" ? -4 : vehicle.vehicle_type === "uav" ? 45 : 0 };
  }
  if (command.type === "waypoint" && command.target) {
    vehicle.mode = "waypoint";
    vehicle.manualWaypoint = true;
    vehicle.target = command.target;
  }
}

function updateDemoVehicleColor(vehicles: DemoVehicle[], vehicleId: string, color: string): void {
  const vehicle = vehicles.find((candidate) => candidate.vehicle_id === vehicleId);
  if (vehicle) {
    vehicle.marker_color = color;
  }
}

function seedForwardDemoWaypoints(vehicles: DemoVehicle[]): void {
  const yp = vehicles.find((vehicle) => vehicle.vehicle_type === "yp");
  if (!yp) {
    return;
  }
  const forwardOffsets = [
    { distance: 120, lateral: -65 },
    { distance: 175, lateral: 55 },
  ];
  const aftOffsets = [
    { distance: 80, lateral: -45 },
    { distance: 115, lateral: 45 },
    { distance: 150, lateral: 0 },
  ];
  let forwardIndex = 0;
  let aftIndex = 0;
  vehicles
    .filter((vehicle) => vehicle.vehicle_type !== "yp")
    .forEach((vehicle) => {
      const useForwardTarget = vehicle.vehicle_type === "uav";
      const offset = useForwardTarget ? forwardOffsets[forwardIndex % forwardOffsets.length] : aftOffsets[aftIndex % aftOffsets.length];
      if (useForwardTarget) {
        forwardIndex += 1;
      } else {
        aftIndex += 1;
      }
      const axisBearing = useForwardTarget ? yp.heading : yp.heading + 180;
      const axisPoint = destinationPoint(yp.lat, yp.lon, axisBearing, offset.distance);
      const target = destinationPoint(axisPoint.latitude, axisPoint.longitude, yp.heading + 90, offset.lateral);
      vehicle.target = {
        latitude: target.latitude,
        longitude: target.longitude,
        altitude: vehicle.vehicle_type === "uuv" ? -7 : vehicle.vehicle_type === "uav" ? vehicle.alt : 0,
      };
      vehicle.mode = "waypoint";
      vehicle.manualWaypoint = true;
    });
}

function randomDemoTarget(lat: number, lon: number, alt: number): DemoVehicle["target"] {
  return {
    latitude: lat + (Math.random() - 0.5) * 0.002,
    longitude: lon + (Math.random() - 0.5) * 0.002,
    altitude: alt,
  };
}

function randomDemoTargetNearYp(yp: DemoVehicle, vehicle: DemoVehicle): DemoVehicle["target"] {
  const bearing = Math.random() * 360;
  const distance = 50 + Math.random() * 130;
  const target = destinationPoint(yp.lat, yp.lon, bearing, distance);
  return {
    latitude: target.latitude,
    longitude: target.longitude,
    altitude: vehicle.vehicle_type === "uuv" ? -6 - Math.random() * 8 : vehicle.vehicle_type === "uav" ? 35 + Math.random() * 25 : 0,
  };
}

function sternTargetForYp(yp: DemoVehicle, vehicle: DemoVehicle): DemoVehicle["target"] {
  const stern = destinationPoint(yp.lat, yp.lon, yp.heading + 180, 35);
  const lateralOffset = vehicle.vehicle_type === "uav" ? 12 : vehicle.vehicle_type === "uuv" ? -12 : 0;
  const target = lateralOffset === 0 ? stern : destinationPoint(stern.latitude, stern.longitude, yp.heading + 90, lateralOffset);
  return {
    latitude: target.latitude,
    longitude: target.longitude,
    altitude: vehicle.vehicle_type === "uuv" ? -5 : vehicle.vehicle_type === "uav" ? 35 : 0,
  };
}

function waypointOffset(lat: number, lon: number, index: number, total: number): { latitude: number; longitude: number } {
  if (total <= 1) {
    return { latitude: lat, longitude: lon };
  }
  const radius = 4 + Math.floor(index / 6) * 3;
  const bearing = (360 / total) * index;
  return destinationPoint(lat, lon, bearing, radius);
}

function haversineMeters(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const radius = 6371000;
  const p1 = (lat1 * Math.PI) / 180;
  const p2 = (lat2 * Math.PI) / 180;
  const dp = ((lat2 - lat1) * Math.PI) / 180;
  const dl = ((lon2 - lon1) * Math.PI) / 180;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return radius * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function bearingDegrees(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const p1 = (lat1 * Math.PI) / 180;
  const p2 = (lat2 * Math.PI) / 180;
  const dl = ((lon2 - lon1) * Math.PI) / 180;
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (Math.atan2(y, x) * 180) / Math.PI;
}

function destinationPoint(lat: number, lon: number, bearing: number, distance: number): { latitude: number; longitude: number } {
  const radius = 6371000;
  const angular = distance / radius;
  const theta = (bearing * Math.PI) / 180;
  const p1 = (lat * Math.PI) / 180;
  const l1 = (lon * Math.PI) / 180;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(angular) + Math.cos(p1) * Math.sin(angular) * Math.cos(theta));
  const l2 = l1 + Math.atan2(Math.sin(theta) * Math.sin(angular) * Math.cos(p1), Math.cos(angular) - Math.sin(p1) * Math.sin(p2));
  return { latitude: (p2 * 180) / Math.PI, longitude: (l2 * 180) / Math.PI };
}

function smoothDegrees(current: number, target: number, ratio: number): number {
  const delta = ((((target - current) % 360) + 540) % 360) - 180;
  return (current + delta * ratio + 360) % 360;
}

function yawToQuaternion(yawDeg: number): Record<string, number> {
  const half = (yawDeg * Math.PI) / 360;
  return { x: 0, y: 0, z: Math.sin(half), w: Math.cos(half) };
}

function vehicleIcon(vehicle: Vehicle, isPhoneViewer: boolean) {
  const type = vehicle.vehicle_type;
  const heading = vehicle.heading ?? 0;
  const altitude = vehicle.position?.altitude ?? 0;
  const color = vehicleMarkerColor(vehicle);
  const lowBattery = vehicle.vehicle_type !== "yp" && (vehicle.battery?.percentage ?? 1) <= LOW_BATTERY_THRESHOLD;
  const hasVideo = vehicle.vehicle_type === "usv";
  const iconScale = isPhoneViewer ? 0.5 : 1;
  const iconSize: [number, number] = [92 * iconScale, 50 * iconScale];
  return L.divIcon({
    className: "",
    iconSize,
    iconAnchor: [iconSize[0] / 2, iconSize[1] / 2],
    html: `
      <div class="marker-wrap${isPhoneViewer ? " phone" : ""}">
        <div class="vehicle-marker ${type}" title="${vehicle.vehicle_id}" style="--vehicle-color: ${color}; transform: rotate(${heading}deg)">
          ${vehicleGlyph(type)}
        </div>
        <div class="alt-label">
          ${altitude.toFixed(0)} m
          ${
            hasVideo
              ? `<span class="video-stream-mark" title="Video stream available"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 10.5v3L21 17V7z"/><rect x="3" y="6" width="12" height="12" rx="2"/></svg></span>`
              : ""
          }
          ${
            lowBattery
              ? `<span class="low-battery-mark" title="Low battery"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 8h15v8H3z"/><path d="M20 10v4"/><path d="M6 11v2"/></svg></span>`
              : ""
          }
        </div>
      </div>
    `,
  });
}

function vehicleGlyph(type: VehicleType): string {
  if (type === "yp") {
    return `
      <svg viewBox="0 0 72 96" aria-hidden="true">
        <path class="v-shadow" d="M36 3 C52 17 62 42 60 76 C54 88 46 93 36 94 C26 93 18 88 12 76 C10 42 20 17 36 3 Z" />
        <path class="v-hull" d="M36 4 C51 18 59 42 56 76 C51 86 44 90 36 91 C28 90 21 86 16 76 C13 42 21 18 36 4 Z" />
        <path class="v-deck" d="M36 13 C47 28 52 50 49 73 C45 80 40 83 36 83 C32 83 27 80 23 73 C20 50 25 28 36 13 Z" />
        <path class="v-panel" d="M26 43 H46 L48 61 H24 Z" />
        <path class="v-window" d="M27 36 H34 V43 H27 Z M38 36 H45 V43 H38 Z" />
        <path class="v-line" d="M22 70 H50 M29 21 L36 9 L43 21 M19 49 L25 47 M53 49 L47 47" />
      </svg>
    `;
  }

  if (type === "uav") {
    return `
      <svg viewBox="0 0 88 88" aria-hidden="true">
        <path class="v-arm" d="M42 42 L18 18 M46 42 L70 18 M42 46 L18 70 M46 46 L70 70" />
        <circle class="v-rotor" cx="15" cy="15" r="9" />
        <circle class="v-rotor" cx="73" cy="15" r="9" />
        <circle class="v-rotor" cx="15" cy="73" r="9" />
        <circle class="v-rotor" cx="73" cy="73" r="9" />
        <path class="v-blade" d="M7 12 C12 8 18 8 23 12 M65 12 C70 8 76 8 81 12 M7 76 C12 80 18 80 23 76 M65 76 C70 80 76 80 81 76" />
        <path class="v-hull" d="M44 20 L54 43 L44 58 L34 43 Z" />
        <path class="v-panel" d="M39 33 H49 V48 H39 Z" />
      </svg>
    `;
  }

  if (type === "uuv") {
    return `
      <svg viewBox="0 0 56 112" aria-hidden="true">
        <path class="v-hull" d="M28 4 C38 15 42 35 42 69 C42 94 36 108 28 108 C20 108 14 94 14 69 C14 35 18 15 28 4 Z" />
        <path class="v-fin" d="M14 70 L3 82 L14 85 Z M42 70 L53 82 L42 85 Z M23 100 L28 111 L33 100 Z" />
        <path class="v-panel" d="M23 25 H33 V38 H23 Z M22 55 H34 V78 H22 Z" />
        <path class="v-line" d="M16 52 H40 M18 91 H38" />
      </svg>
    `;
  }

  return `
    <svg viewBox="0 0 72 96" aria-hidden="true">
      <path class="v-hull" d="M36 5 C50 19 56 41 54 78 C49 87 43 91 36 91 C29 91 23 87 18 78 C16 41 22 19 36 5 Z" />
      <path class="v-deck" d="M36 17 C45 31 49 51 47 72 C43 78 39 80 36 80 C33 80 29 78 25 72 C23 51 27 31 36 17 Z" />
      <path class="v-panel" d="M28 45 H44 L45 58 H27 Z" />
      <path class="v-line" d="M23 73 H49 M28 27 L36 15 L44 27" />
    </svg>
  `;
}

function vehicleColor(type: VehicleType): string {
  return {
    uav: "#dc2626",
    uavf: "#b91c1c",
    usv: "#16a34a",
    uuv: "#eab308",
    yp: "#6b7280",
  }[type];
}

function vehicleMarkerColor(vehicle: Vehicle): string {
  return (vehicle as DemoVehicleWithStyle).marker_color ?? vehicleColor(vehicle.vehicle_type);
}

function withLocalVehicleColor(vehicle: Vehicle, localColors: Record<string, string>): Vehicle {
  const color = localColors[vehicle.vehicle_id];
  return color ? ({ ...vehicle, marker_color: color } as DemoVehicleWithStyle) : vehicle;
}

function vehicleZIndexOffset(type: VehicleType): number {
  return {
    uav: 4000,
    uavf: 4000,
    yp: 3000,
    usv: 2000,
    uuv: 1000,
  }[type];
}

function assignedVehicleColor(type: VehicleType, typeIndex: number): string {
  return lightenHex(vehicleColor(type), Math.min(typeIndex * 0.18, 0.5));
}

function lightenHex(hex: string, amount: number): string {
  const clean = hex.replace("#", "");
  const red = parseInt(clean.slice(0, 2), 16);
  const green = parseInt(clean.slice(2, 4), 16);
  const blue = parseInt(clean.slice(4, 6), 16);
  const mix = (value: number) => Math.round(value + (255 - value) * amount);
  return `#${[mix(red), mix(green), mix(blue)].map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}
