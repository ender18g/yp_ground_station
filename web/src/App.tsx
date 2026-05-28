import L from "leaflet";
import {
  AlertTriangle,
  Battery,
  Brush,
  Cable,
  CheckCircle2,
  CircleDashed,
  Crosshair,
  EthernetPort,
  Grid3X3,
  Layers,
  Loader2,
  LocateFixed,
  Maximize2,
  MessageSquare,
  Plus,
  RotateCcw,
  Route,
  Settings,
  Ship,
  Trash2,
  Video,
  Wifi,
  WifiOff,
  X,
  Map as MapIcon
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, Suspense, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { Circle, CircleMarker, MapContainer, Marker, Polyline, Popup, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";

import { connectSITL, disconnectSITL, fetchSettings, listSITLBridges, sendCommand, triggerMOB, updateSettings, websocketUrl } from "./api";
import type { SITLBridge } from "./api";
import type { Command, Vehicle, VehicleType } from "./types";
import { Canvas } from "@react-three/fiber";
import { useGLTF, OrbitControls, Environment, Sphere, Line } from "@react-three/drei";


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

interface LocalWaypoint {
  id: string;
  x: number;
  y: number;
  z: number;
}

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
  "#dc2626", "#ef4444", "#f97316", "#f59e0b", "#eab308", "#84cc16",
  "#16a34a", "#14b8a6", "#06b6d4", "#0ea5e9", "#2563eb", "#4f46e5",
  "#7c3aed", "#c026d3", "#db2777", "#6b7280",
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
  const [mapZoom, setMapZoom] = useState(17);
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
  const [mobError, setMobError] = useState<string | null>(null);
  const [showSITL, setShowSITL] = useState(false);
  const [sitlBridges, setSitlBridges] = useState<Record<string, SITLBridge>>({});
  const [sarPatterns, setSarPatterns] = useState<Record<string, { patternType: string; waypoints: [number, number][] }>>({});
  const followBeforeWaypointDragRef = useRef(false);
  const wsRef = useRef<WebSocket | null>(null);
  const demoSimsRef = useRef<DemoVehicle[]>([]);
  const localVehicleColorsRef = useRef<Record<string, string>>({});
  
  const [activeTab, setActiveTab] = useState<"map" | "planner">("map");

  useEffect(() => {
    if (!DEMO_MODE || !("serviceWorker" in navigator)) {
      return;
    }
    navigator.serviceWorker.register(`${import.meta.env.BASE_URL}tile-cache-sw.js`).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (DEMO_MODE) return;
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
        if (payload.op === "sitl_bridge_update") {
          const bridge = payload.bridge as SITLBridge;
          setSitlBridges((current) => ({ ...current, [bridge.vehicle_id]: bridge }));
        }
        if (payload.op === "sitl_bridge_removed") {
          setSitlBridges((current) => {
            const next = { ...current };
            delete next[payload.vehicle_id as string];
            return next;
          });
        }
        if (payload.op === "sar_pattern") {
          setSarPatterns((current) => ({
            ...current,
            [payload.vehicle_id as string]: {
              patternType: payload.pattern_type as string,
              waypoints: payload.waypoints as [number, number][],
            },
          }));
        }
        if (payload.op === "sar_pattern_cleared") {
          setSarPatterns((current) => {
            const next = { ...current };
            delete next[payload.vehicle_id as string];
            return next;
          });
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
    if (DEMO_MODE) return;
    listSITLBridges()
      .then((bridges) =>
        setSitlBridges(Object.fromEntries(bridges.map((b) => [b.vehicle_id, b])))
      )
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!DEMO_MODE) return;
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
    if (DEMO_MODE) return;
    let cancelled = false;
    fetchSettings()
      .then((serverSettings) => {
        if (cancelled) return;
        setMessageRetentionMinutes(Math.round(serverSettings.message_retention_seconds / 60));
        setSettingsLoaded(true);
      })
      .catch(() => setSettingsLoaded(true));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (DEMO_MODE || !settingsLoaded) return;
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
    setMobError(null);
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

      if (result.ok) {
        setMobModalOpen(false);
      } else {
        setMobError(result.error ?? "Dispatch failed");
      }
    } catch (err) {
      setMobError(err instanceof Error ? err.message : "Network error");
    }
    setMobSending(false);
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
      
      {activeTab === "map" ? (
        <MapContainer center={center} zoom={17} minZoom={3} maxZoom={19} zoomControl className="map">
          <TileLayer key={`${mapBase}-${renderedMapSource}`} url={mapLayer.url} attribution={mapLayer.attribution} />
          <MapZoomTracker onZoom={setMapZoom} />
          <MapCommander
            onMapAction={(lat, lon, point) => setMapActionMenu({ lat, lon, x: point.x, y: point.y })}
          />
          <MapPanTracker onManualPan={() => setFollowYp(false)} />
          <FollowYpCenter yp={yp} enabled={followYp} />
          <FitAllControl vehicles={vehicleList} />
          {showYpRangeRings && <YpRangeRings yp={yp} />}
          {(Object.entries(sarPatterns) as Array<[string, { patternType: string; waypoints: [number, number][] }]>).map(([vehicleId, pattern]) => (
            <SarPatternOverlay
              key={vehicleId}
              vehicleId={vehicleId}
              patternType={pattern.patternType}
              waypoints={pattern.waypoints}
              color={(vehicles[vehicleId] as DemoVehicleWithStyle | undefined)?.marker_color ?? "#f97316"}
              onClear={() => setSarPatterns((prev) => { const next = { ...prev }; delete next[vehicleId]; return next; })}
            />
          ))}
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
              mapZoom={mapZoom}
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
      ) : (
        <WaypointPlanner 
           yp={yp} 
           vehicles={vehicleList.filter(v => v.vehicle_type !== "yp")} 
           onCommand={command} 
        />
      )}

      {activeTab === "map" && mapActionMenu && (
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

      {activeTab === "map" && (
        <MapMenu mapBase={mapBase} mapSource={mapSource} onMapBaseChange={setMapBase} onMapSourceChange={setMapSource} />
      )}

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
          <button
            className={activeTab === "map" ? "icon-button active" : "icon-button"}
            title="Global Map"
            onClick={() => setActiveTab("map")}
          >
            <MapIcon size={19} />
          </button>
          
          <button
            className={activeTab === "planner" ? "icon-button active" : "icon-button"}
            title="Local Waypoint Planner"
            onClick={() => { setActiveTab("planner"); setShowSettings(false); setShowSITL(false); }}
            >
            <Crosshair size={19} />
          </button>
          <button
            className={showSITL ? "icon-button active" : "icon-button"}
            title="SITL connections"
            onClick={() => { setShowSITL((v) => !v); setShowSettings(false); }}
          >
            <Cable size={19} />
          </button>
          <button
            className={showSettings ? "icon-button active" : "icon-button"}
            title="Settings"
            onClick={() => { setShowSettings((value) => !value); setShowSITL(false); }}
          >
            <Settings size={19} />
          </button>
          <button className="icon-button" title="Messages" onClick={() => setShowMessages((value) => !value)}>
            <MessageSquare size={19} />
          </button>
        </div>
      </div>

      {showSITL && !DEMO_MODE && (
        <SITLPanel
          bridges={sitlBridges}
          onConnect={(url, vehicleId) =>
            connectSITL(url, vehicleId || undefined)
              .then((result) => {
                if (!result.ok) return;
              })
              .catch(() => undefined)
          }
          onDisconnect={(vehicleId) =>
            disconnectSITL(vehicleId)
              .then(() =>
                setSitlBridges((current) => {
                  const next = { ...current };
                  delete next[vehicleId];
                  return next;
                })
              )
              .catch(() => undefined)
          }
        />
      )}

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
            {mobError && (
              <div className="mob-modal-error">
                <AlertTriangle size={14} /> {mobError}
              </div>
            )}
            <div className="mob-modal-actions">
              <button
                className="mob-cancel-btn"
                onClick={() => { setMobModalOpen(false); setMobError(null); }}
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

// ============================================================================
// WAYPOINT PLANNER COMPONENTS
// ============================================================================
export function WaypointPlanner({ yp, vehicles, onCommand }: { yp?: Vehicle, vehicles: Vehicle[], onCommand: (vehicleId: string, cmd: Command) => void }) {
  const [waypoints, setWaypoints] = useState<LocalWaypoint[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedVehicleId, setSelectedVehicleId] = useState<string>("");

  const updateWaypoint = (id: string, updates: Partial<LocalWaypoint>) => {
    setWaypoints((prev) => prev.map(wp => wp.id === id ? { ...wp, ...updates } : wp));
  };

  const deleteWaypoint = (id: string) => {
    setWaypoints((prev) => prev.filter(wp => wp.id !== id));
    if (selectedId === id) setSelectedId(null);
  };

  const handleDispatch = () => {
    if (!yp?.position || yp.heading == null) {
      alert("Cannot dispatch: YP GPS or heading is unavailable.");
      return;
    }
    if (!selectedVehicleId) {
      alert("Please select a vehicle to dispatch.");
      return;
    }

    const globalWaypoints = waypoints.map(wp => 
      localToGlobalWaypoint(yp.position!.latitude, yp.position!.longitude, yp.heading!, yp.position!.altitude || 0, wp.x, wp.y, wp.z)
    );

    globalWaypoints.forEach((gWp) => {
      onCommand(selectedVehicleId, { 
        type: "waypoint", 
        target: { latitude: gWp.latitude, longitude: gWp.longitude, altitude: gWp.altitude } 
      });
    });

    alert(`Dispatched ${globalWaypoints.length} waypoints to ${selectedVehicleId}`);
    setWaypoints([]);
    setSelectedId(null);
  };

  const linePoints = waypoints.map((wp) => [wp.x, wp.z, wp.y] as [number, number, number]);

  return (
    <div className="planner-container" style={{ display: "flex", flexDirection: "column", width: "100%", height: "100%", overflowY: "auto", backgroundColor: "#0f172a", color: "white", paddingTop: 60, paddingBottom: 40 }}>
      
      {/* TOP PANEL: 3D Render Area */}
      <div style={{ flex: "0 0 auto", height: "45vh", minHeight: 350, position: 'relative', borderBottom: '2px solid #334155' }}>
        <Canvas camera={{ position: [60, 50, 60], fov: 45 }}>
          <ambientLight intensity={0.5} />
          <directionalLight position={[10, 10, 5]} intensity={1.5} />
          <Suspense fallback={
            <mesh position={[0, 0, 0]}><boxGeometry args={[2, 2, 2]} /><meshStandardMaterial color="#f97316" wireframe /></mesh>
          }>
            <ShipModel modelUrl="/logos/YP_CAD.glb" />
          </Suspense>
          
          {/* WAYPOINTS: Made 3x larger and glowing so they stand out */}
          {waypoints.map((wp) => (
            <Sphere key={wp.id} position={[wp.x, wp.z, wp.y]} args={[1.5, 16, 16]}>
              <meshStandardMaterial 
                 color={wp.id === selectedId ? "#38bdf8" : "#ef4444"} 
                 emissive={wp.id === selectedId ? "#38bdf8" : "#ef4444"}
                 emissiveIntensity={0.6}
              />
            </Sphere>
          ))}

          {/* Thickened the line so you can see the path clearly */}
          {linePoints.length > 1 && <Line points={linePoints} color="#f59e0b" lineWidth={5} />}
          
          <OrbitControls makeDefault target={[0, 0, 0]} />
          
          {/* Expanded the grid floor to visually match your 150m x 150m 2D workspace */}
          <gridHelper args={[150, 150, "#334155", "#1e293b"]} position={[0, -2, 0]} />
        </Canvas>
      </div>

      {/* BOTTOM PANEL: Split 2D View and Altitude View */}
      <div style={{ flex: "0 0 auto", minHeight: 500, display: "flex", flexWrap: "wrap" }}>
        
        {/* BOTTOM LEFT: 2D Top-Down View */}
        <div style={{ flex: "1 1 400px", padding: 20, borderRight: '2px solid #334155', display: "flex", flexDirection: "column" }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10 }}>
            <h2 style={{ fontSize: "1.2rem", fontWeight: "bold" }}>Lateral Planner (Top-Down)</h2>
            
            {/* FIX: The button is always rendered now, but uses opacity to hide itself. Layout is locked! */}
            <button 
              onClick={() => selectedId && deleteWaypoint(selectedId)} 
              disabled={!selectedId}
              style={{ 
                padding: "4px 8px", background: "#ef4444", color: "white", border: "none", 
                borderRadius: "4px", cursor: selectedId ? "pointer" : "default", 
                display: "flex", alignItems: "center", gap: 5,
                opacity: selectedId ? 1 : 0, 
                pointerEvents: selectedId ? "auto" : "none",
                transition: "opacity 0.2s" // Adds a nice smooth fade in
              }}
            >
              <Trash2 size={14} /> Delete Selected
            </button>

          </div>
          
          <div style={{ flex: 1, display: "flex", justifyContent: "center", alignItems: "center" }}>
            <InteractiveWaypoint2D 
              waypoints={waypoints} 
              selectedId={selectedId}
              onSelect={setSelectedId}
              onAdd={(x, y) => {
                const newId = Date.now().toString();
                setWaypoints(prev => [...prev, { id: newId, x, y, z: 15 }]);
                setSelectedId(newId);
              }}
              onUpdate={updateWaypoint}
            />
          </div>
        </div>

        {/* BOTTOM RIGHT: Altitude Profile & Dispatch Controls */}
        <div style={{ flex: "1 1 400px", padding: 20, display: "flex", flexDirection: "column" }}>
          <h2 style={{ fontSize: "1.2rem", fontWeight: "bold", marginBottom: 10 }}>Altitude Profile</h2>
          
          <div style={{ flex: 1, minHeight: 250, backgroundColor: "#1e293b", borderRadius: 8, border: "1px solid #475569", position: "relative", marginBottom: 15, padding: "10px 0" }}>
             <AltitudeProfile 
                waypoints={waypoints} 
                selectedId={selectedId} 
                onSelect={setSelectedId}
                onUpdateAltitude={(id, z) => updateWaypoint(id, { z })}
             />
          </div>

          <div style={{ display: "flex", gap: 10 }}>
            <select value={selectedVehicleId} onChange={(e) => setSelectedVehicleId(e.target.value)} style={{ flex: 1, background: "#1e293b", color: "white", border: "1px solid #475569", padding: "10px", borderRadius: "4px" }}>
              <option value="">-- Select Vehicle --</option>
              {vehicles.map(v => <option key={v.vehicle_id} value={v.vehicle_id}>{v.vehicle_id} ({v.vehicle_type})</option>)}
            </select>
            <button onClick={() => { setWaypoints([]); setSelectedId(null); }} style={{ padding: "10px", background: "#475569", color: "white", border: "none", borderRadius: "4px", cursor: "pointer" }}>Clear All</button>
            <button onClick={handleDispatch} style={{ padding: "10px", background: "#2563eb", color: "white", border: "none", borderRadius: "4px", cursor: "pointer", fontWeight: "bold" }}>Dispatch</button>
          </div>
        </div>

      </div>
    </div>
  );
}

function InteractiveWaypoint2D({ waypoints, selectedId, onSelect, onAdd, onUpdate }: { 
  waypoints: LocalWaypoint[]; 
  selectedId: string | null;
  onSelect: (id: string) => void;
  onAdd: (x: number, y: number) => void;
  onUpdate: (id: string, updates: Partial<LocalWaypoint>) => void;
}) {
  // NEW: We define a workspace that represents 150x150 meters in the real world
  const WORKSPACE_WIDTH_M = 150; 
  const WORKSPACE_HEIGHT_M = 150; 
  const SHIP_LENGTH_METERS = 33; 
  const SHIP_WIDTH_METERS = 8;   
  
  const containerRef = useRef<HTMLDivElement>(null);

  const handlePointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!containerRef.current) return;
    if (e.target === containerRef.current) {
      const rect = containerRef.current.getBoundingClientRect();
      
      // Update math to use the new workspace boundaries
      const xMeters = ((e.clientX - rect.left) / rect.width - 0.5) * WORKSPACE_WIDTH_M; 
      const yMeters = -((e.clientY - rect.top) / rect.height - 0.5) * WORKSPACE_HEIGHT_M; 
      onAdd(xMeters, yMeters);
    }
  };

  const startDrag = (id: string, e: ReactPointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    onSelect(id);
    const target = e.currentTarget;
    target.setPointerCapture(e.pointerId);

    const move = (moveEvent: PointerEvent) => {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const clampedX = Math.max(0, Math.min(moveEvent.clientX - rect.left, rect.width));
      const clampedY = Math.max(0, Math.min(moveEvent.clientY - rect.top, rect.height));
      
      // Update drag math to use the new workspace boundaries
      const xMeters = ((clampedX / rect.width) - 0.5) * WORKSPACE_WIDTH_M;
      const yMeters = -((clampedY / rect.height) - 0.5) * WORKSPACE_HEIGHT_M;
      onUpdate(id, { x: xMeters, y: yMeters });
    };

    const up = () => {
      target.releasePointerCapture(e.pointerId);
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
    };

    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
  };

  return (
    <div 
      ref={containerRef}
      onPointerDown={handlePointerDown}
      style={{ 
        width: "100%", maxWidth: 400, aspectRatio: "1/1", // Forces it to be a nice big square
        backgroundColor: '#0f172a', 
        backgroundImage: "linear-gradient(#334155 1px, transparent 1px), linear-gradient(90deg, #334155 1px, transparent 1px)", // Adds a grid texture
        backgroundSize: '20px 20px',
        backgroundPosition: 'center center',
        position: 'relative', cursor: 'crosshair', border: '2px solid #475569', borderRadius: 4,
        overflow: 'hidden'
      }}
    >
      {/* THE SHIP OVERLAY: Positioned perfectly in the center, properly scaled against the 150m grid */}
      <div style={{
        position: 'absolute',
        top: '50%', left: '50%',
        width: `${(SHIP_WIDTH_METERS / WORKSPACE_WIDTH_M) * 100}%`,
        height: `${(SHIP_LENGTH_METERS / WORKSPACE_HEIGHT_M) * 100}%`,
        transform: 'translate(-50%, -50%)',
        backgroundImage: "url('/logos/YP.png')",
        backgroundSize: 'contain',
        backgroundPosition: 'center',
        backgroundRepeat: 'no-repeat',
        pointerEvents: 'none', // Critical: Lets you click "through" the transparent parts of the ship PNG
        opacity: 0.8
      }} />

      {/* WAYPOINT DOTS */}
      {waypoints.map((wp, index) => {
        // Update plotting math to use the new workspace boundaries
        const pctX = (wp.x / WORKSPACE_WIDTH_M) + 0.5;
        const pctY = (-wp.y / WORKSPACE_HEIGHT_M) + 0.5;
        const isSelected = wp.id === selectedId;
        
        return (
          <div 
            key={wp.id} 
            onPointerDown={(e) => startDrag(wp.id, e)}
            style={{ 
              position: 'absolute', left: `${pctX * 100}%`, top: `${pctY * 100}%`, 
              width: 18, height: 18, 
              backgroundColor: isSelected ? '#38bdf8' : '#ef4444', 
              border: isSelected ? '2px solid white' : '1px solid #7f1d1d',
              borderRadius: '50%', transform: 'translate(-50%, -50%)', 
              cursor: 'grab', zIndex: isSelected ? 10 : 1,
              display: 'flex', justifyContent: 'center', alignItems: 'center', fontSize: 10, color: 'white', fontWeight: 'bold',
              userSelect: 'none'
            }} 
          >
            {index + 1}
          </div>
        );
      })}
    </div>
  );
}

function AltitudeProfile({ waypoints, selectedId, onSelect, onUpdateAltitude }: {
  waypoints: LocalWaypoint[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onUpdateAltitude: (id: string, newAlt: number) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const MAX_ALTITUDE = 50;

  const startDrag = (id: string, e: ReactPointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    onSelect(id);
    const target = e.currentTarget;
    target.setPointerCapture(e.pointerId);

    const move = (moveEvent: PointerEvent) => {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const clampedY = Math.max(0, Math.min(moveEvent.clientY - rect.top, rect.height));
      
      const altPercentage = 1 - (clampedY / rect.height);
      const newAlt = altPercentage * MAX_ALTITUDE;
      onUpdateAltitude(id, Math.max(0, newAlt));
    };

    const up = () => {
      target.releasePointerCapture(e.pointerId);
      target.removeEventListener("pointermove", move as EventListener);
      target.removeEventListener("pointerup", up as EventListener);
    };

    target.addEventListener("pointermove", move as EventListener);
    target.addEventListener("pointerup", up as EventListener);
  };

  if (waypoints.length === 0) {
    return <div style={{ padding: 20, color: "#64748b", textAlign: "center", height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>Click the 2D map to add waypoints.</div>;
  }

  const points = waypoints.map((wp, index) => {
    const pctX = waypoints.length === 1 ? 50 : (index / (waypoints.length - 1)) * 90 + 5; 
    const pctY = (1 - (wp.z / MAX_ALTITUDE)) * 100;
    return { id: wp.id, x: `${pctX}%`, y: `${pctY}%`, z: wp.z, index: index + 1 };
  });

  const polylinePoints = points.map(p => `${p.x.replace('%','')} ${p.y.replace('%','')}`).join(', ');

  return (
    <div ref={containerRef} style={{ width: "100%", height: "100%", position: "relative", touchAction: "none" }}>
      <div style={{ position: "absolute", top: "0%", width: "100%", borderTop: "1px dashed #334155" }}><span style={{fontSize: 10, color: '#64748b', paddingLeft: 5}}>50m</span></div>
      <div style={{ position: "absolute", top: "50%", width: "100%", borderTop: "1px dashed #334155" }}><span style={{fontSize: 10, color: '#64748b', paddingLeft: 5}}>25m</span></div>
      <div style={{ position: "absolute", bottom: "0%", width: "100%", borderTop: "1px solid #475569" }}><span style={{fontSize: 10, color: '#64748b', paddingLeft: 5}}>0m</span></div>

      {/* SVG is now ONLY used for the connecting line */}
      <svg width="100%" height="100%" style={{ display: "block" }} preserveAspectRatio="none" viewBox="0 0 100 100">
        {points.length > 1 && (
          <polyline points={polylinePoints} fill="none" stroke="#f59e0b" strokeWidth="1" vectorEffect="non-scaling-stroke" />
        )}
      </svg>
      
      {/* HTML Divs are used for the dots, ensuring they never stretch or scale weirdly */}
      {points.map((p) => {
        const isSelected = p.id === selectedId;
        return (
          <div 
            key={p.id} 
            onPointerDown={(e) => startDrag(p.id, e)}
            style={{ 
              position: 'absolute', 
              left: p.x, 
              top: p.y, 
              width: 18, 
              height: 18, 
              backgroundColor: isSelected ? '#38bdf8' : '#ef4444', 
              border: isSelected ? '2px solid white' : '1px solid #7f1d1d',
              borderRadius: '50%', 
              transform: 'translate(-50%, -50%)', 
              cursor: 'ns-resize', 
              zIndex: isSelected ? 10 : 1,
              display: 'flex', 
              justifyContent: 'center', 
              alignItems: 'center', 
              fontSize: 10, 
              color: 'white', 
              fontWeight: 'bold',
              userSelect: 'none'
            }} 
          >
            {p.index}
          </div>
        );
      })}
    </div>
  );
}


// ============================================================================
// OLD APP LOGIC FUNCTIONS
// ============================================================================

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

function SITLPanel({
  bridges,
  onConnect,
  onDisconnect,
}: {
  bridges: Record<string, SITLBridge>;
  onConnect: (url: string, vehicleId: string) => void;
  onDisconnect: (vehicleId: string) => void;
}) {
  const [url, setUrl] = useState("");
  const [vehicleId, setVehicleId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);

  const handleConnect = async () => {
    const trimUrl = url.trim();
    if (!trimUrl) {
      setError("MAVLink URL is required");
      return;
    }
    const validPrefixes = ["tcp:", "tcpin:", "tcpout:", "udpin:", "udpout:", "udpbcast:", "serial:"];
    if (!validPrefixes.some((p) => trimUrl.toLowerCase().startsWith(p))) {
      setError(`URL must start with: ${validPrefixes.join(", ")}`);
      return;
    }
    setError(null);
    setConnecting(true);
    const result = await connectSITL(trimUrl, vehicleId.trim() || undefined).catch((e) => ({
      ok: false as const,
      error: String(e),
    }));
    setConnecting(false);
    if (!result.ok) {
      setError(result.error ?? "Connection failed");
    } else {
      setUrl("");
      setVehicleId("");
    }
  };

  const bridgeList = Object.values(bridges);

  return (
    <div className="sitl-panel">
      <div className="panel-title">
        <Cable size={17} />
        <strong>SITL Connections</strong>
      </div>

      <div className="sitl-form">
        <label className="sitl-field-label">MAVLink URL</label>
        <input
          className="sitl-input"
          type="text"
          placeholder="tcp:localhost:5760"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !connecting && handleConnect()}
          spellCheck={false}
        />
        <label className="sitl-field-label">Vehicle ID <span className="sitl-optional">(optional)</span></label>
        <input
          className="sitl-input"
          type="text"
          placeholder="auto-generated from URL"
          value={vehicleId}
          onChange={(e) => setVehicleId(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !connecting && handleConnect()}
          spellCheck={false}
        />
        {error && <div className="sitl-error">{error}</div>}
        <button className="sitl-connect-btn" onClick={handleConnect} disabled={connecting}>
          {connecting ? <Loader2 size={15} className="sitl-spin" /> : <Plus size={15} />}
          {connecting ? "Connecting…" : "Connect"}
        </button>
      </div>

      {bridgeList.length > 0 && (
        <div className="sitl-bridge-list">
          {bridgeList.map((bridge) => (
            <div key={bridge.vehicle_id} className={`sitl-bridge-row sitl-status-${bridge.status}`}>
              <div className="sitl-bridge-icon">
                {bridge.status === "connected" && <CheckCircle2 size={15} />}
                {bridge.status === "connecting" && <Loader2 size={15} className="sitl-spin" />}
                {bridge.status === "error" && <CircleDashed size={15} />}
                {bridge.status === "disconnected" && <CircleDashed size={15} />}
              </div>
              <div className="sitl-bridge-info">
                <strong>{bridge.vehicle_id}</strong>
                <span className="sitl-bridge-meta">
                  {bridge.frame ? `${bridge.frame}` : bridge.url}
                  {bridge.autopilot ? ` · ${bridge.autopilot}` : ""}
                </span>
                {bridge.status === "error" && bridge.error && (
                  <span className="sitl-bridge-error">{bridge.error}</span>
                )}
              </div>
              <button
                className="sitl-disconnect-btn"
                title="Disconnect"
                onClick={() => onDisconnect(bridge.vehicle_id)}
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      )}

      {bridgeList.length === 0 && (
        <div className="sitl-empty">No active connections. Enter a MAVLink URL above to connect a SITL instance.</div>
      )}

      <div className="sitl-hint">
        Examples: <code>tcp:localhost:5760</code> · <code>udpin:0.0.0.0:14551</code>
      </div>
    </div>
  );
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
  mapZoom,
  onClick,
}: {
  vehicle: Vehicle;
  trailSeconds: number;
  isPhoneViewer: boolean;
  mapZoom: number;
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
        icon={vehicleIcon(vehicle, isPhoneViewer, mapZoom)}
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

function MapZoomTracker({ onZoom }: { onZoom: (zoom: number) => void }) {
  useMapEvents({
    zoomend: (event) => onZoom(event.target.getZoom()),
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

function SarPatternOverlay({
  vehicleId,
  patternType,
  waypoints,
  color,
  onClear,
}: {
  vehicleId: string;
  patternType: string;
  waypoints: [number, number][];
  color: string;
  onClear: () => void;
}) {
  if (waypoints.length < 2) return null;
  const label = patternType === "mob" ? "MOB Search" : "Grid Search";
  return (
    <>
      <Polyline
        positions={waypoints}
        pathOptions={{ color, weight: 2, opacity: 0.85, dashArray: "6 4" }}
      />
      <CircleMarker
        center={waypoints[0]}
        radius={6}
        pathOptions={{ color, fillColor: color, fillOpacity: 1, weight: 1.5 }}
      >
        <Tooltip permanent direction="top" offset={[0, -8]} className="sar-label-tooltip">
          {label} — {vehicleId}
        </Tooltip>
        <Popup className="sar-clear-popup">
          <div className="sar-clear-popup-inner">
            <span>{label}</span>
            <span className="sar-clear-popup-vehicle">{vehicleId}</span>
            <button className="sar-clear-btn" onClick={onClear}>Clear pattern</button>
          </div>
        </Popup>
      </CircleMarker>
      <CircleMarker
        center={waypoints[waypoints.length - 1]}
        radius={4}
        pathOptions={{ color, fillColor: "#fff", fillOpacity: 1, weight: 2 }}
      />
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

function ShipModel({ modelUrl }: { modelUrl: string }) {
  const { scene } = useGLTF(modelUrl);
  
  return (
    <primitive 
      object={scene} 
      scale={0.01} 
      rotation={[-Math.PI / 2, 0, 0]} 
      position={[0, -2, 5]} 
    />
  );
}

useGLTF.preload("/logos/YP_CAD.glb");

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

function localToGlobalWaypoint(shipLat: number, shipLon: number, shipHeading: number, shipAlt: number, localX: number, localY: number, localZ: number) {
  const distanceMeters = Math.hypot(localX, localY);
  const relativeAngleRad = Math.atan2(localX, localY);
  const relativeAngleDeg = (relativeAngleRad * 180) / Math.PI;
  const trueBearing = (shipHeading + relativeAngleDeg + 360) % 360;
  
  const globalCoord = destinationPoint(shipLat, shipLon, trueBearing, distanceMeters);
  
  return {
    latitude: globalCoord.latitude,
    longitude: globalCoord.longitude,
    altitude: shipAlt + localZ
  };
}

function smoothDegrees(current: number, target: number, ratio: number): number {
  const delta = ((((target - current) % 360) + 540) % 360) - 180;
  return (current + delta * ratio + 360) % 360;
}

function yawToQuaternion(yawDeg: number): Record<string, number> {
  const half = (yawDeg * Math.PI) / 360;
  return { x: 0, y: 0, z: Math.sin(half), w: Math.cos(half) };
}

function vehicleIcon(vehicle: Vehicle, isPhoneViewer: boolean, zoom: number) {
  const type = vehicle.vehicle_type;
  const heading = vehicle.heading ?? 0;
  const altitude = vehicle.position?.altitude ?? 0;
  const color = vehicleMarkerColor(vehicle);
  const lowBattery = vehicle.vehicle_type !== "yp" && (vehicle.battery?.percentage ?? 1) <= LOW_BATTERY_THRESHOLD;
  const hasVideo = vehicle.vehicle_type === "usv";
  
  const baseSizes: Record<string, [number, number]> = {
    yp:  [120, 60],
    usv: [70, 35],
    ugv: [70, 35],
    uuv: [60, 30],
    uav: [50, 50],
  };

  const baseSize = baseSizes[type] ?? [60, 30];

  const scale = Math.pow(2, zoom - 17);
  const phoneScale = isPhoneViewer ? 0.6 : 1;
  
  const finalWidth = Math.round(baseSize[0] * scale * phoneScale);
  const finalHeight = Math.round(baseSize[1] * scale * phoneScale);
  const iconSize: [number, number] = [finalWidth, finalHeight];

  return L.divIcon({
    className: "", 
    iconSize,
    iconAnchor: [Math.round(finalWidth / 2), Math.round(finalHeight / 2)], 
    html: `
      <div class="marker-wrap${isPhoneViewer ? " phone" : ""}" style="position: absolute; top: 0; left: 0; margin: 0; padding: 0; width: ${finalWidth}px; height: ${finalHeight}px;">
        
        <div class="vehicle-marker ${type}" title="${vehicle.vehicle_id}" style="position: absolute; top: 0; left: 0; margin: 0; padding: 0; width: 100%; height: 100%; --vehicle-color: ${color}; transform: rotate(${heading}deg); transform-origin: center center; display: flex; align-items: center; justify-content: center;">
          ${vehicleGlyph(type)}
        </div>
        
        <div class="alt-label" style="position: absolute; bottom: -24px; left: 50%; transform: translateX(-50%); white-space: nowrap; margin: 0; padding: 0;">
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
  const baseUrl = import.meta.env.BASE_URL;
  
  const iconPaths: Record<string, string> = {
    yp: `${baseUrl}logos/YP.png`,
    uav: `${baseUrl}logos/MultiRotor.png`,
    uavf: `${baseUrl}logos/fixedWing.png`,
    usv: `${baseUrl}logos/USV_orange.png`,
    ugv: `${baseUrl}logos/UGV.png`,
    uuv: `${baseUrl}logos/UUV.png`,
  };

  const src = iconPaths[type] ?? iconPaths.uav;

  return `<img src="${src}" alt="${type}" style="display: block; margin: 0; padding: 0; max-width: 100%; max-height: 100%; width: 100%; height: 100%; object-fit: contain; pointer-events: none;" />`;
}

function vehicleColor(type: VehicleType): string {
  return {
    uav: "#dc2626",
    uavf: "#b91c1c",
    usv: "#16a34a",
    ugv: "#b45309",
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
    uav: 1500,
    uavf: 1500,
    yp: 4000,
    usv: 2000,
    ugv: 1800,
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

// ============================================================================
// MISSING UI COMPONENTS (VehicleModal, UsvVideoViewer, Tooltips)
// ============================================================================

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