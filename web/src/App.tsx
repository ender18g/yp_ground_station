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
  Radio,
  RotateCcw,
  Route,
  Settings,
  Ship,
  Trash2,
  Video,
  Wifi,
  WifiOff,
  X,
  Map as MapIcon,
  LogOut,
  Users,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, Suspense, type ChangeEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { Circle, CircleMarker, MapContainer, Marker, Polyline, Popup, TileLayer, Tooltip, WMSTileLayer, useMap, useMapEvents } from "react-leaflet";
import { createPortal } from "react-dom";

import { connectSITL, disconnectSITL, fetchSettings, getCurrentUser, listSITLBridges, listSerialPorts, sendCommand, setYpRole, triggerMOB, updateSettings, websocketUrl, isAuthenticated, logout as logoutUser, fetchDeconflictionSettings, updateDeconflictionSettings } from "./api";
import type { CurrentUser, SITLBridge, SerialPortInfo } from "./api";
import type { Command, Position, RelativeWaypoint, Vehicle, VehicleType } from "./types";
import Login from "./Login";
import UserManagement from "./UserManagement";
import { Canvas } from "@react-three/fiber";
import { useGLTF, OrbitControls, Environment, Sphere, Line } from "@react-three/drei";


const USNA_CENTER: [number, number] = [38.9822, -76.4819];
const MAX_MESSAGE_LOG = 700;
const DEMO_MODE = import.meta.env.VITE_STATIC_DEMO === "true" || window.location.pathname.startsWith("/demo") || window.location.search.includes("demo=true");
/** View-only mode: live data but commands blocked for real (non-sim) vehicles. */
const VIEW_MODE = !DEMO_MODE && (window.location.pathname.startsWith("/view") || window.location.search.includes("view=true"));
/** Returns true if a vehicle ID belongs to a docker-spawned sim vehicle. */
function isSimVehicle(vehicleId: string): boolean {
  return vehicleId.startsWith("sim-");
}
const YP_DEMO_SPEED_MPS = 5 * 0.514444;
const YP_DEMO_HEADING = 330;
const DEMO_KEEP_IN_RANGE_M = 200;
const LOW_BATTERY_THRESHOLD = 0.25;
const BRAND_LOGO_URL = `${import.meta.env.BASE_URL}logos/usna_crest_jhublue.png`;
const WEATHER_RADAR_WMS_URL = "https://mapservices.weather.noaa.gov/eventdriven/services/radar/radar_base_reflectivity/MapServer/WMSServer";
const WEATHER_RADAR_REFRESH_MS = 10 * 60 * 1000;
const WEATHER_RADAR_OPACITY = 0.48;
const TRANSPARENT_TILE_DATA_URL =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=";
const WIND_OVERLAY_REFRESH_MS = 15 * 60 * 1000;
const WIND_OVERLAY_SOURCE = "Open-Meteo";
const WIND_OVERLAY_ATTRIBUTION = `Wind &copy; ${WIND_OVERLAY_SOURCE}`;
const WIND_SAMPLE_COLUMNS = 4;
const WIND_SAMPLE_ROWS = 3;
const WIND_FETCH_TIMEOUT_MS = 7000;

/** Mode availability by vehicle type (ArduPilot-based vehicles and PX4) */
const VEHICLE_MODES: Record<string, string[]> = {
  uav: ["STABILIZE", "ACRO", "ALT_HOLD", "AUTO", "GUIDED", "LOITER", "RTL", "CIRCLE", "LAND", "DRIFT", "SPORT", "FLIP", "AUTOTUNE", "POSHOLD"],
  usv: ["MANUAL", "GUIDED", "AUTO", "RTL", "LOITER", "CIRCLE"],
  ugv: ["MANUAL", "GUIDED", "AUTO", "RTL", "LOITER", "CIRCLE"],
  uavf: ["MANUAL", "ALTITUDE_CONTROL", "POSITION_CONTROL", "AUTO", "OFFBOARD", "EMERGENCY"],
  uuv: ["MANUAL", "GUIDED", "AUTO", "RTL", "LOITER"],
  yp: [],
};

type MapBase = "satellite" | "street";
type MapSource = "auto" | "cache" | "online";

interface WindSample {
  id: string;
  latitude: number;
  longitude: number;
  speedKmh: number;
  directionDeg: number;
}

type ProjectedWindSample = WindSample & { x: number; y: number };

interface WindState {
  samples: WindSample[];
  projectedSamples: ProjectedWindSample[];
}

interface YpReadout {
  headingDeg?: number;
  speedKts?: number;
}

interface LocalWaypoint {
  id: string;
  x: number;
  y: number;
  z: number;
}

interface MissionPlannerWaypoint {
  id: string;
  latitude: number;
  longitude: number;
  altitude: number;
  itemType: "waypoint" | "takeoff" | "loiter_time" | "land" | "rtl" | "do_jump";
  commandIdOverride: number | null;
  param3: number;
  jumpTargetIndex: number;
  jumpRepeatCount: number;
  holdTimeS: number;
  acceptanceRadiusM: number;
  yawDeg: number | null;
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
  trackingYP?: boolean;
}

const VEHICLE_COLOR_PALETTE = [
  "#dc2626", "#ef4444", "#f97316", "#f59e0b", "#eab308", "#84cc16",
  "#16a34a", "#14b8a6", "#06b6d4", "#0ea5e9", "#2563eb", "#4f46e5",
  "#7c3aed", "#c026d3", "#db2777", "#6b7280",
];

export function App() {
  const [isLoggedIn, setIsLoggedIn] = useState(isAuthenticated());

  if (!isLoggedIn) {
    return <Login onLogin={() => setIsLoggedIn(true)} />;
  }

  return <GroundStation onLogout={() => { logoutUser(); setIsLoggedIn(false); }} />;
}

function GroundStation({ onLogout }: { onLogout: () => void }) {
  const isPhoneViewer = useIsPhoneViewer();
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
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
  const [showWeatherRadar, setShowWeatherRadar] = useState(false);
  const [showWindOverlay, setShowWindOverlay] = useState(false);
  const [mapSource, setMapSource] = useState<MapSource>(DEMO_MODE ? "online" : "auto");
  const [mapMenuExpanded, setMapMenuExpanded] = useState(false);
  const [mapZoom, setMapZoom] = useState(17);
  const [mapCenter, setMapCenter] = useState<[number, number]>(USNA_CENTER);
  const [followYp, setFollowYp] = useState(true);
  const [showYpRangeRings, setShowYpRangeRings] = useState(true);
  const [messageRetentionMinutes, setMessageRetentionMinutes] = useState(10);
  const [rtbUpdateHz, setRtbUpdateHz] = useState(2.0);
  const [rtbSternDistanceM, setRtbSternDistanceM] = useState(35);
  const [settingsLoaded, setSettingsLoaded] = useState(DEMO_MODE);
  const [mapActionMenu, setMapActionMenu] = useState<MapActionMenuState | null>(null);
  const [streamVehicleId, setStreamVehicleId] = useState<string | null>(null);
  const [preferredWaypointVehicleId, setPreferredWaypointVehicleId] = useState<string | null>(null);
  const [waypointMarkers, setWaypointMarkers] = useState<Record<string, WaypointMarker>>({});
  const [mobModalOpen, setMobModalOpen] = useState(false);
  const [mobSending, setMobSending] = useState(false);
  const [mobError, setMobError] = useState<string | null>(null);
  const [mobVehicleId, setMobVehicleId] = useState<string>("");
  const [mobTrackSeconds, setMobTrackSeconds] = useState(120);
  const [mobSwathM, setMobSwathM] = useState(20);
  const [mobAltM, setMobAltM] = useState(30);
  const [mobCorridorHalfWidthM, setMobCorridorHalfWidthM] = useState(50);
  const [mobTakeoffAltitudeM, setMobTakeoffAltitudeM] = useState(30);
  const [mobClimbSpeedMs, setMobClimbSpeedMs] = useState(8);
  const [settingsTab, setSettingsTab] = useState<"display" | "mob" | "vessel" | "deconfliction">("display");
  const [deconflictionEnabled, setDeconflictionEnabled] = useState(false);
  const [deconflictionGlobalRadius, setDeconflictionGlobalRadius] = useState(10.0);
  const [deconflictionRadii, setDeconflictionRadii] = useState<Record<string, number>>({
    uav: 10.0,
    usv: 15.0,
    ugv: 15.0,
    uuv: 15.0,
    yp: 20.0,
  });
  const [deconflictionOrbitRadius, setDeconflictionOrbitRadius] = useState(50.0);
  const [deconflictionMaxPause, setDeconflictionMaxPause] = useState(300.0);
  const [showSITL, setShowSITL] = useState(false);
  const [showUserManagement, setShowUserManagement] = useState(false);
  const [sitlBridges, setSitlBridges] = useState<Record<string, SITLBridge>>({});
  const [ypRoleVehicleId, setYpRoleVehicleId] = useState<string | null>(null);
  const [sarPatterns, setSarPatterns] = useState<Record<string, { patternType: string; waypoints: [number, number][] }>>({});
  const [missionPlans, setMissionPlans] = useState<Record<string, [number, number][]>>({});
  const [sarMissionActiveByVehicle, setSarMissionActiveByVehicle] = useState<Record<string, boolean>>({});
  const followBeforeWaypointDragRef = useRef(false);
  const wsRef = useRef<WebSocket | null>(null);
  const demoSimsRef = useRef<DemoVehicle[]>([]);
  const localVehicleColorsRef = useRef<Record<string, string>>({});
  const settingsPanelRef = useRef<HTMLDivElement | null>(null);
  const sitlPanelRef = useRef<HTMLDivElement | null>(null);
  const messageDrawerRef = useRef<HTMLDivElement | null>(null);
  const mapMenuRef = useRef<HTMLDivElement | null>(null);
  const settingsButtonRef = useRef<HTMLButtonElement | null>(null);
  const sitlButtonRef = useRef<HTMLButtonElement | null>(null);
  const messagesButtonRef = useRef<HTMLButtonElement | null>(null);
  const mapMenuToggleRef = useRef<HTMLButtonElement | null>(null);
  
  const [activeTab, setActiveTab] = useState<"map" | "mission" | "planner">("map");

  useEffect(() => {
    let cancelled = false;
    void getCurrentUser().then((user) => {
      if (cancelled) return;
      if (!user) {
        onLogout();
        return;
      }
      setCurrentUser(user);
    });
    return () => {
      cancelled = true;
    };
  }, [onLogout]);

  const updateSarMissionState = (vehicleId: string, commandType: string) => {
    setSarMissionActiveByVehicle((current) => {
      const next = { ...current };
      if (commandType === "search_grid" || commandType === "mob") {
        next[vehicleId] = true;
      } else if (commandType === "cancel_sar" || commandType === "rtb" || commandType === "waypoint") {
        next[vehicleId] = false;
      }
      return next;
    });
  };

  useEffect(() => {
    if (!DEMO_MODE || !("serviceWorker" in navigator)) {
      return;
    }
    navigator.serviceWorker.register(`${import.meta.env.BASE_URL}tile-cache-sw.js`).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (DEMO_MODE) return;
    let disposed = false;
    let retry: number | undefined;

    const connect = () => {
      const ws = new WebSocket(websocketUrl("/ws/ui"));
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onclose = (event) => {
        setConnected(false);
        if (disposed) return;
        if (event.code === 4001) {
          onLogout();
          return;
        }
        retry = window.setTimeout(connect, 1500);
      };
      ws.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (payload.op === "snapshot") {
          const snapshotVehicles = payload.vehicles as Vehicle[];
          setVehicles(Object.fromEntries(snapshotVehicles.map((vehicle) => [vehicle.vehicle_id, withLocalVehicleColor(vehicle, localVehicleColorsRef.current)])));
          setMessageLog(snapshotMessages(snapshotVehicles).slice(0, MAX_MESSAGE_LOG));
          setWaypointMarkers(Object.fromEntries((payload.waypoints as WaypointMarker[] | undefined ?? []).map((waypoint) => [waypoint.vehicle_id, waypoint])));
          setSarPatterns(Object.fromEntries(Object.entries(payload.sar_patterns as Record<string, { pattern_type: string; waypoints: [number, number][] }> | undefined ?? {}).map(([vehicleId, pattern]) => [vehicleId, { patternType: pattern.pattern_type, waypoints: pattern.waypoints }])));
          setMissionPlans(payload.mission_plans as Record<string, [number, number][]> ?? {});
        }
        if (payload.op === "vehicle_update") {
          const incoming = withLocalVehicleColor(payload.vehicle as Vehicle, localVehicleColorsRef.current);
          setVehicles((current) => {
            const prev = current[incoming.vehicle_id];
            const prevHistory: Position[] = prev?.history ?? [];
            const msgType: string = (payload.message as { type?: string } | undefined)?.type ?? "";
            const pos = incoming.position;
            const stamp: number | undefined = (payload.message as { stamp?: number } | undefined)?.stamp;
            const newHistory: Position[] =
              msgType.includes("NavSatFix") && pos
                ? [...prevHistory, { latitude: pos.latitude, longitude: pos.longitude, altitude: pos.altitude, stamp }].slice(-500)
                : prevHistory;
            
            return { 
              ...current, 
              [incoming.vehicle_id]: { 
                ...incoming, 
                history: newHistory 
              } 
            };
          });
          
          if (payload.message) {
            setMessageLog((current) => [streamMessageFromPayload(payload.message), ...current].slice(0, MAX_MESSAGE_LOG));
          }
        }
        if (payload.op === "command_ack") {
          setMessageLog((current) => [streamMessageFromCommandAck(payload), ...current].slice(0, MAX_MESSAGE_LOG));
            const ackVehicleId = payload.vehicle_id as string | undefined;
            const ackCommandType = (payload.command as { type?: string } | undefined)?.type;
            if (ackVehicleId && ackCommandType) {
              updateSarMissionState(ackVehicleId, ackCommandType);
            }
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
        if (payload.op === "vehicle_removed") {
          const removedId = payload.vehicle_id as string;
          setVehicles((current) => {
            const next = { ...current };
            delete next[removedId];
            return next;
          });
          setSarMissionActiveByVehicle((current) => {
            const next = { ...current };
            delete next[removedId];
            return next;
          });
          setSitlBridges((current) => {
            const next = { ...current };
            delete next[removedId];
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
        if (payload.op === "waypoint_overlay") {
          const waypoint = payload.waypoint as WaypointMarker;
          setWaypointMarkers((current) => ({ ...current, [waypoint.vehicle_id]: waypoint }));
        }
        if (payload.op === "mission_plan_overlay") {
          setMissionPlans((current) => ({ ...current, [payload.vehicle_id as string]: payload.waypoints as [number, number][] }));
        }
        if (payload.op === "mission_plan_cleared") {
          setMissionPlans((current) => {
            const next = { ...current };
            delete next[payload.vehicle_id as string];
            return next;
          });
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
        if (payload.op === "video_stream_update") {
          const incoming = payload.video as Vehicle["video"] & { vehicle_id?: string };
          const vehicleId = incoming?.vehicle_id;
          if (!vehicleId) {
            return;
          }
          setVehicles((current) => {
            const currentVehicle = current[vehicleId];
            if (!currentVehicle) {
              return current;
            }
            return {
              ...current,
              [vehicleId]: {
                ...currentVehicle,
                video: incoming,
              },
            };
          });
        }
        if (payload.op === "video_stream_removed") {
          const vehicleId = payload.vehicle_id as string;
          setVehicles((current) => {
            const currentVehicle = current[vehicleId];
            if (!currentVehicle || !currentVehicle.video) {
              return current;
            }
            const nextVehicle = { ...currentVehicle };
            delete nextVehicle.video;
            return { ...current, [vehicleId]: nextVehicle };
          });
        }
      };
    };

    connect();
    return () => {
      disposed = true;
      window.clearTimeout(retry);
      wsRef.current?.close();
    };
  }, [onLogout]);

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
        if (typeof serverSettings.trail_seconds === "number") {
          setTrailSeconds(serverSettings.trail_seconds);
        }
        if (typeof serverSettings.show_yp_range_rings === "boolean") {
          setShowYpRangeRings(serverSettings.show_yp_range_rings);
        }
        setMessageRetentionMinutes(Math.round(serverSettings.message_retention_seconds / 60));
        if (typeof serverSettings.rtb_update_hz === "number") {
          setRtbUpdateHz(serverSettings.rtb_update_hz);
        }
        if (typeof serverSettings.rtb_stern_distance_m === "number") {
          setRtbSternDistanceM(serverSettings.rtb_stern_distance_m);
        }
        setYpRoleVehicleId(serverSettings.yp_role_vehicle_id ?? null);
        if (typeof serverSettings.mob_track_seconds === "number") {
          setMobTrackSeconds(serverSettings.mob_track_seconds);
        }
        if (typeof serverSettings.mob_swath_m === "number") {
          setMobSwathM(serverSettings.mob_swath_m);
        }
        if (typeof serverSettings.mob_altitude_m === "number") {
          setMobAltM(serverSettings.mob_altitude_m);
        }
        if (typeof serverSettings.mob_corridor_half_width_m === "number") {
          setMobCorridorHalfWidthM(serverSettings.mob_corridor_half_width_m);
        }
        if (typeof serverSettings.mob_takeoff_altitude_m === "number") {
          setMobTakeoffAltitudeM(serverSettings.mob_takeoff_altitude_m);
        }
        if (typeof serverSettings.mob_climb_speed_ms === "number") {
          setMobClimbSpeedMs(serverSettings.mob_climb_speed_ms);
        }
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
      updateSettings({
        trail_seconds: trailSeconds,
        show_yp_range_rings: showYpRangeRings,
        message_retention_seconds: messageRetentionMinutes * 60,
        rtb_update_hz: rtbUpdateHz,
        rtb_stern_distance_m: rtbSternDistanceM,
        mob_track_seconds: mobTrackSeconds,
        mob_swath_m: mobSwathM,
        mob_altitude_m: mobAltM,
        mob_corridor_half_width_m: mobCorridorHalfWidthM,
        mob_takeoff_altitude_m: mobTakeoffAltitudeM,
        mob_climb_speed_ms: mobClimbSpeedMs,
        yp_role_vehicle_id: ypRoleVehicleId,
      }).catch(() => undefined);
    }, 350);
    return () => window.clearTimeout(timeout);
  }, [trailSeconds, showYpRangeRings, messageRetentionMinutes, rtbUpdateHz, rtbSternDistanceM, mobTrackSeconds, mobSwathM, mobAltM, mobCorridorHalfWidthM, mobTakeoffAltitudeM, mobClimbSpeedMs, ypRoleVehicleId, settingsLoaded]);

  useEffect(() => {
    if (DEMO_MODE) return;
    let cancelled = false;
    fetchDeconflictionSettings()
      .then((settings) => {
        if (cancelled) return;
        setDeconflictionEnabled(settings.enabled);
        setDeconflictionGlobalRadius(settings.global_radius_m);
        if (settings.radius_per_type && Object.keys(settings.radius_per_type).length > 0) {
          setDeconflictionRadii((current) => ({ ...current, ...settings.radius_per_type }));
        }
        setDeconflictionOrbitRadius(settings.orbit_radius_m);
        setDeconflictionMaxPause(settings.max_pause_duration_s);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (DEMO_MODE) return;
    const timeout = window.setTimeout(() => {
      updateDeconflictionSettings({
        enabled: deconflictionEnabled,
        global_radius_m: deconflictionGlobalRadius,
        radius_per_type: deconflictionRadii,
        orbit_radius_m: deconflictionOrbitRadius,
        max_pause_duration_s: deconflictionMaxPause,
      }).catch(() => undefined);
    }, 500);
    return () => window.clearTimeout(timeout);
  }, [deconflictionEnabled, deconflictionGlobalRadius, deconflictionRadii, deconflictionOrbitRadius, deconflictionMaxPause]);

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (!target) {
        return;
      }

      if (showSettings) {
        const insideSettingsPanel = settingsPanelRef.current?.contains(target) ?? false;
        const onSettingsButton = settingsButtonRef.current?.contains(target) ?? false;
        if (!insideSettingsPanel && !onSettingsButton) {
          setShowSettings(false);
        }
      }

      if (showSITL) {
        const insideSITLPanel = sitlPanelRef.current?.contains(target) ?? false;
        const onSITLButton = sitlButtonRef.current?.contains(target) ?? false;
        if (!insideSITLPanel && !onSITLButton) {
          setShowSITL(false);
        }
      }

      if (showMessages) {
        const insideMessageDrawer = messageDrawerRef.current?.contains(target) ?? false;
        const onMessagesButton = messagesButtonRef.current?.contains(target) ?? false;
        if (!insideMessageDrawer && !onMessagesButton) {
          setShowMessages(false);
        }
      }

      if (mapMenuExpanded) {
        const insideMapMenu = mapMenuRef.current?.contains(target) ?? false;
        const onMapMenuToggle = mapMenuToggleRef.current?.contains(target) ?? false;
        if (!insideMapMenu && !onMapMenuToggle) {
          setMapMenuExpanded(false);
        }
      }
    };

    window.addEventListener("pointerdown", onPointerDown);
    return () => window.removeEventListener("pointerdown", onPointerDown);
  }, [showSettings, showSITL, showMessages, mapMenuExpanded]);

  const vehicleList = useMemo(() => Object.values(vehicles).filter((vehicle) => vehicle.position), [vehicles]);
  const yp = vehicleList.find((vehicle) => vehicle.vehicle_type === "yp");
  const ypGpsLinked = Boolean(yp?.connected);
  const center: [number, number] = yp?.position ? [yp.position.latitude, yp.position.longitude] : USNA_CENTER;
  const filteredMessages = useMemo(() => filterMessages(messageLog, topicFilters), [messageLog, topicFilters]);
  const renderedMapSource = DEMO_MODE ? "online" : mapSource;
  const mapLayer = useMemo(() => tileLayerFor(mapBase, renderedMapSource), [mapBase, renderedMapSource]);

  const command = (vehicleId: string, body: Command) => {
    updateSarMissionState(vehicleId, body.type);
    if (DEMO_MODE) {
      handleDemoCommand(demoSimsRef.current, vehicleId, body);
      return;
    }
    if (VIEW_MODE && !isSimVehicle(vehicleId)) {
      return; // Block commands to real vehicles in view-only mode
    }
    sendCommand(wsRef.current, vehicleId, body);
  };

  const sendWaypoint = (vehicleId: string, lat: number, lon: number, altitude?: number) => {
    // Right-click "Send Vehicle" should interrupt any active SAR mission first.
    command(vehicleId, { type: "cancel_sar" });
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
      const result = await triggerMOB(mobVehicleId || undefined, mobTrackSeconds, mobSwathM, mobAltM, mobCorridorHalfWidthM, mobTakeoffAltitudeM, mobClimbSpeedMs);
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
        updateSarMissionState(vehicleId, "mob");
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
    const commandableVehicles = vehicleList.filter((candidate) => {
      if (candidate.vehicle_type === "yp") return false;
      if (VIEW_MODE && !isSimVehicle(candidate.vehicle_id)) return false;
      return true;
    });
    const nextMarkers: Record<string, WaypointMarker> = {};
    commandableVehicles.forEach((vehicle, index) => {
      const offset = waypointOffset(lat, lon, index, commandableVehicles.length);
      command(vehicle.vehicle_id, { type: "cancel_sar" });
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
        <MapContainer center={mapCenter} zoom={mapZoom} minZoom={3} maxZoom={20} zoomControl className="map">
          <TileLayer key={`${mapBase}-${renderedMapSource}`} url={mapLayer.url} attribution={mapLayer.attribution} maxNativeZoom={mapLayer.maxNativeZoom} maxZoom={20} />
                    {showWeatherRadar && <WeatherRadarLayer />}
                    <WindLayer yp={yp} showVectors={showWindOverlay} onToggleVectors={() => setShowWindOverlay((value) => !value)} />
          <MapZoomTracker onZoom={setMapZoom} />
          <MapCommander
            onMapAction={(lat, lon, point) => setMapActionMenu({ lat, lon, x: point.x, y: point.y })}
          />
          <MapPanTracker onManualPan={() => setFollowYp(false)} onPan={setMapCenter} />
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
              onClear={() => command(vehicleId, { type: "clear_sar_pattern" })}
            />
          ))}
          {Object.entries(missionPlans).map(([vehicleId, waypoints]) => (
            waypoints.length > 1 && <Polyline key={`mission-${vehicleId}`} positions={waypoints} pathOptions={{ color: "#2563eb", weight: 3, opacity: 0.9 }} />
          ))}
          {Object.values(waypointMarkers)
            .filter((waypoint) => !VIEW_MODE || isSimVehicle(waypoint.vehicle_id))
            .map((waypoint) => (
            <WaypointCrosshair
              key={waypoint.vehicle_id}
              waypoint={waypoint}
              vehicle={vehicles[waypoint.vehicle_id]}
              yp={yp}
              onClick={() => {
                const selectedVehicle = vehicles[waypoint.vehicle_id];
                if (!selectedVehicle) return;
                setMapActionMenu(null);
                if (selectedVehicle.vehicle_type === "yp") {
                  setFollowYp(true);
                }
                setSelected(selectedVehicle);
              }}
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
      ) : activeTab === "mission" ? (
        <MissionPlannerMode
          center={mapCenter}
          zoom={mapZoom}
          onZoomChange={setMapZoom}
          onCenterChange={setMapCenter}
          mapLayer={mapLayer}
          vehicles={vehicleList}
          missionPlans={missionPlans}
          canCommandVehicle={(vehicleId) => !VIEW_MODE || isSimVehicle(vehicleId)}
          onCommand={command}
        />
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
          vehicles={VIEW_MODE ? vehicleList.filter((v) => v.vehicle_type === "yp" || isSimVehicle(v.vehicle_id)) : vehicleList}
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

      {activeTab !== "planner" && (
        <MapMenu
          mapBase={mapBase}
          mapSource={mapSource}
          expanded={mapMenuExpanded}
          setMenuRef={(node) => {
            mapMenuRef.current = node;
          }}
          setToggleRef={(node) => {
            mapMenuToggleRef.current = node;
          }}
          onExpandedChange={setMapMenuExpanded}
          onMapBaseChange={setMapBase}
          onMapSourceChange={setMapSource}
          showWeatherRadar={showWeatherRadar}
          showWindOverlay={showWindOverlay}
          onWeatherRadarChange={setShowWeatherRadar}
          onWindOverlayChange={setShowWindOverlay}
        />
      )}

      <div className="trident-tagline">Telemetry, Remote Intelligence, Data, Electronic Navigation, and Tasking — Yard Patrol</div>

      <div className="topbar">
        <div className="brand">
          <img className="brand-logo" src={BRAND_LOGO_URL} alt="USNA crest" />
          <div className="brand-copy">
            <strong>TRIDENT YP Vehicle View</strong>
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
              {VIEW_MODE && (
                <span className="brand-status view-only">
                  <Radio size={15} />
                  View only
                </span>
              )}
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
            className={activeTab === "mission" ? "icon-button active" : "icon-button"}
            title="Mission Planner"
            onClick={() => { setActiveTab("mission"); setShowSettings(false); setShowSITL(false); }}
          >
            <Route size={19} />
          </button>
          
          <button
            className={activeTab === "planner" ? "icon-button active" : "icon-button"}
            title="Local Waypoint Planner"
            onClick={() => { setActiveTab("planner"); setShowSettings(false); setShowSITL(false); }}
            >
            <Crosshair size={19} />
          </button>
          {!VIEW_MODE && (
            <button
              ref={sitlButtonRef}
              className={showSITL ? "icon-button active" : "icon-button"}
              title="Vehicle Connections"
              onClick={() => { setShowSITL((v) => !v); setShowSettings(false); }}
            >
              <Cable size={19} />
            </button>
          )}
          <button
            ref={settingsButtonRef}
            className={showSettings ? "icon-button active" : "icon-button"}
            title="Settings"
            onClick={() => { setShowSettings((value) => !value); setShowSITL(false); }}
          >
            <Settings size={19} />
          </button>
          <button ref={messagesButtonRef} className="icon-button" title="Messages" onClick={() => setShowMessages((value) => !value)}>
            <MessageSquare size={19} />
          </button>
          {currentUser?.permissions.includes("manage_users") && (
            <button
              className={showUserManagement ? "icon-button active" : "icon-button"}
              title="User Management"
              onClick={() => setShowUserManagement((value) => !value)}
            >
              <Users size={19} />
            </button>
          )}
          <button className="icon-button" title="Logout" onClick={onLogout}>
            <LogOut size={19} />
          </button>
        </div>
      </div>

      {showSITL && !DEMO_MODE && (
        <div ref={sitlPanelRef}>
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
        </div>
      )}

      {showUserManagement && currentUser?.permissions.includes("manage_users") && (
        <UserManagement onClose={() => setShowUserManagement(false)} />
      )}

      {showSettings && (
        <div className="settings-panel" ref={settingsPanelRef}>
          <div className="panel-title">
            <Settings size={17} />
            <strong>Settings</strong>
          </div>
          <div className="settings-tabs">
            <button
              className={settingsTab === "display" ? "settings-tab active" : "settings-tab"}
              onClick={() => setSettingsTab("display")}
            >
              Display
            </button>
            <button
              className={settingsTab === "deconfliction" ? "settings-tab active" : "settings-tab"}
              onClick={() => setSettingsTab("deconfliction")}
            >
              Deconfliction
            </button>
            <button
              className={settingsTab === "mob" ? "settings-tab active" : "settings-tab"}
              onClick={() => setSettingsTab("mob")}
            >
              Man Overboard
            </button>
            <button
              className={settingsTab === "vessel" ? "settings-tab active" : "settings-tab"}
              onClick={() => setSettingsTab("vessel")}
            >
              Vessel
            </button>
          </div>          {settingsTab === "display" && (
            <>
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
            </>
          )}
          {settingsTab === "deconfliction" && (
            <>
              <label className="setting-toggle">
                <span>Enable vehicle deconfliction</span>
                <input 
                  type="checkbox" 
                  checked={deconflictionEnabled} 
                  onChange={(event) => setDeconflictionEnabled(event.target.checked)} 
                />
              </label>
              <p className="settings-hint">
                Automatically detect and resolve collisions between vehicles using mission priority hierarchy.
                MOB missions have highest priority, followed by Search Grid, Mission Planner, and Waypoints.
              </p>
              
              <label>
                Global safety radius
                <span>{deconflictionGlobalRadius.toFixed(1)} m</span>
              </label>
              <input 
                min={1} 
                max={50} 
                step={0.5} 
                type="range" 
                value={deconflictionGlobalRadius}
                disabled={!deconflictionEnabled}
                onChange={(event) => setDeconflictionGlobalRadius(Number(event.target.value))}
              />
              
              <label>Radius per vehicle type</label>
              {Object.entries(deconflictionRadii).map(([vehicleType, radius]) => (
                <div key={vehicleType} className="deconfliction-radius-row">
                  <label>
                    <span>{vehicleType}</span>
                    <input 
                      min={1} 
                      max={50} 
                      step={0.5} 
                      type="range" 
                      value={radius}
                      disabled={!deconflictionEnabled}
                      onChange={(event) => setDeconflictionRadii((current) => ({ 
                        ...current, 
                        [vehicleType]: Number(event.target.value)
                      }))}
                    />
                    <span>{radius.toFixed(1)}m</span>
                  </label>
                </div>
              ))}
              
              <label>
                Orbit radius for avoidance
                <span>{deconflictionOrbitRadius.toFixed(1)} m</span>
              </label>
              <input 
                min={10} 
                max={200} 
                step={5} 
                type="range" 
                value={deconflictionOrbitRadius}
                disabled={!deconflictionEnabled}
                onChange={(event) => setDeconflictionOrbitRadius(Number(event.target.value))}
              />
              
              <label>
                Max pause duration before warning
                <span>{deconflictionMaxPause.toFixed(0)} s</span>
              </label>
              <input 
                min={10} 
                max={600} 
                step={10} 
                type="range" 
                value={deconflictionMaxPause}
                disabled={!deconflictionEnabled}
                onChange={(event) => setDeconflictionMaxPause(Number(event.target.value))}
              />
            </>
          )}
          {settingsTab === "vessel" && (
            <>
              <label>YP vessel role</label>
              <p className="settings-hint">
                Designate any connected vehicle (e.g. a BlueBoat) to act as the YP mother vessel.
                Its type will be overridden to &ldquo;yp&rdquo;, enabling range rings, MOB track
                recording, and ship-relative commands.
              </p>
              <select
                value={ypRoleVehicleId ?? ""}
                onChange={(e) => {
                  const newId = e.target.value || null;
                  setYpRoleVehicleId(newId);
                  setYpRole(newId).catch(() => undefined);
                }}
              >
                <option value="">— dedicated yp_gps service —</option>
                {Object.values(vehicles)
                  .filter((v) => v.connected && (v.vehicle_type !== "yp" || v.vehicle_id === ypRoleVehicleId))
                  .map((v) => (
                    <option key={v.vehicle_id} value={v.vehicle_id}>
                      {v.vehicle_id} ({v.vehicle_type})
                    </option>
                  ))}
              </select>
              <label>
                RTB update rate
                <span>{rtbUpdateHz.toFixed(1)} Hz</span>
              </label>
              <input
                min={0.2}
                max={10}
                step={0.1}
                type="range"
                value={rtbUpdateHz}
                disabled={DEMO_MODE}
                onChange={(event) => setRtbUpdateHz(Number(event.target.value))}
              />
              <label>
                RTB stern distance
                <span>{rtbSternDistanceM} m</span>
              </label>
              <input min={5} max={200} step={5} type="range" value={rtbSternDistanceM} disabled={DEMO_MODE} onChange={(event) => setRtbSternDistanceM(Number(event.target.value))} />
            </>
          )}
          {settingsTab === "mob" && (
            <>
              <label>
                Track length
                <span>{mobTrackSeconds}s</span>
              </label>
              <input min={10} max={600} step={10} type="range" value={mobTrackSeconds} onChange={(e) => setMobTrackSeconds(Number(e.target.value))} />
              <label>
                Swath width
                <span>{mobSwathM} m</span>
              </label>
              <input min={5} max={100} step={5} type="range" value={mobSwathM} onChange={(e) => setMobSwathM(Number(e.target.value))} />
              <label>
                Search altitude
                <span>{mobAltM} m</span>
              </label>
              <input min={5} max={120} step={5} type="range" value={mobAltM} onChange={(e) => setMobAltM(Number(e.target.value))} />
              <label>
                Search corridor half-width
                <span>{mobCorridorHalfWidthM} m</span>
              </label>
              <input min={10} max={200} step={5} type="range" value={mobCorridorHalfWidthM} onChange={(e) => setMobCorridorHalfWidthM(Number(e.target.value))} />
              <label>
                Takeoff altitude
                <span>{mobTakeoffAltitudeM} m</span>
              </label>
              <input min={5} max={120} step={5} type="range" value={mobTakeoffAltitudeM} onChange={(e) => setMobTakeoffAltitudeM(Number(e.target.value))} />
              <label>
                Climb speed
                <span>{mobClimbSpeedMs.toFixed(1)} m/s</span>
              </label>
              <input min={0.5} max={20} step={0.5} type="range" value={mobClimbSpeedMs} onChange={(e) => setMobClimbSpeedMs(Number(e.target.value))} />
            </>
          )}
        </div>
      )}

      {showMessages && (
        <div ref={messageDrawerRef}>
          <MessageDrawer
            messages={messageLog}
            filteredMessages={filteredMessages}
            filters={topicFilters}
            width={messagePanelWidth}
            onClose={() => setShowMessages(false)}
            onResize={setMessagePanelWidth}
            onFiltersChange={setTopicFilters}
          />
        </div>
      )}

      {selected && (
        <VehicleModal
          // Lookup the live vehicle data, fallback to the snapshot if it briefly disconnects
          vehicle={vehicles[selected.vehicle_id] || selected}
          shipVehicle={yp}
          sarMissionActive={Boolean(sarMissionActiveByVehicle[selected.vehicle_id])}
          canCommand={!VIEW_MODE || isSimVehicle(selected.vehicle_id)}
          onClose={() => setSelected(null)}
          onRtb={() => {
            command(selected.vehicle_id, { type: "cancel_sar" });
            command(selected.vehicle_id, { type: "rtb" });
            // Extract the coordinates into strictly typed local variables first
            const ypLat = yp?.position?.latitude;
            const ypLon = yp?.position?.longitude;

            // Snap the waypoint marker to the YP and lock it
            if (ypLat !== undefined && ypLon !== undefined) {
              setWaypointMarkers((current) => ({
                ...current,
                [selected.vehicle_id]: {
                  vehicle_id: selected.vehicle_id,
                  latitude: ypLat,
                  longitude: ypLon,
                  trackingYP: true,
                }
              }));
            }

            setSelected(null);
          }}
          onEndSar={() => {
            command(selected.vehicle_id, { type: "cancel_sar" });
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
          onSetMode={(mode) => {
            command(selected.vehicle_id, { type: "set_mode", mode });
            setSelected(null);
          }}
        />
      )}

      {/*
      {streamVehicleId && (
        <UsvVideoViewer
          vehicleId={streamVehicleId}
          src={vehicles[streamVehicleId]?.video?.playback_url ?? `${import.meta.env.BASE_URL}media/usv-stream.mp4`}
          onClose={() => setStreamVehicleId(null)}
        />
      )}
        */}
      {streamVehicleId && (
        <UsvVideoViewer
          vehicleId={streamVehicleId}
          // Prefer the dynamic streams array; otherwise turn the canonical
          // server-published playback_url into a single displayable stream.
          streams={
            vehicles[streamVehicleId]?.video?.streams ??
            (vehicles[streamVehicleId]?.video?.playback_url
              ? [{ label: "Camera", url: vehicles[streamVehicleId]!.video!.playback_url! }]
              : [{
                  label: "Default Stream",
                  url: `http://192.168.0.126:8889/${streamVehicleId}/whep`,
                }])
          }
          onClose={() => setStreamVehicleId(null)}
        />
      )}

      {/* Fixed red MOB button — always visible, bottom-right corner */}
      <button
        className="mob-button"
        title="Man Overboard — dispatch SAR search"
        onClick={(e) => {
          e.stopPropagation();
          const commandable = Object.values(vehicles).filter((v) => {
            if (v.vehicle_type === "yp" || v.vehicle_type === "ugv") return false;
            if (VIEW_MODE && !isSimVehicle(v.vehicle_id)) return false;
            return v.connected !== false;
          });
          setMobVehicleId(commandable[0]?.vehicle_id ?? "");
          setMobError(null);
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
              This will immediately dispatch the selected vehicle to search
              the YP vessel&apos;s recent track. Confirm only if a person is overboard.
            </div>
            <div className="mob-modal-vehicle">
              <label className="mob-vehicle-label">Dispatch vehicle</label>
              {(() => {
                const commandable = Object.values(vehicles).filter((v) => {
                  if (v.vehicle_type === "yp" || v.vehicle_type === "ugv") return false;
                  if (VIEW_MODE && !isSimVehicle(v.vehicle_id)) return false;
                  return v.connected !== false;
                });
                return commandable.length > 0 ? (
                  <select
                    className="mob-vehicle-select"
                    value={mobVehicleId}
                    onChange={(e) => setMobVehicleId(e.target.value)}
                    disabled={mobSending}
                  >
                    <option value="">— nearest available —</option>
                    {commandable.map((v) => (
                      <option key={v.vehicle_id} value={v.vehicle_id}>{v.vehicle_id}</option>
                    ))}
                  </select>
                ) : (
                  <div className="mob-no-vehicles">No connected vehicles — server will choose automatically</div>
                );
              })()}
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
function downloadTextFile(filename: string, content: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function qgcCommandIdForItemType(itemType: MissionPlannerWaypoint["itemType"]): number {
  return {
    waypoint: 16,
    loiter_time: 19,
    rtl: 20,
    land: 21,
    takeoff: 22,
    do_jump: 177,
  }[itemType];
}

function itemTypeForCommandId(commandId: number): MissionPlannerWaypoint["itemType"] {
  if (commandId === 22) return "takeoff";
  if (commandId === 19) return "loiter_time";
  if (commandId === 21) return "land";
  if (commandId === 20) return "rtl";
  if (commandId === 177) return "do_jump";
  return "waypoint";
}

function missionWaypointsToQgcPlan(
  waypoints: MissionPlannerWaypoint[],
  defaultAltitude: number,
): Record<string, unknown> {
  return {
    fileType: "Plan",
    geoFence: { polygons: [], circles: [], version: 2 },
    rallyPoints: { points: [], version: 2 },
    version: 1,
    mission: {
      cruiseSpeed: 10,
      firmwareType: 12,
      hoverSpeed: 5,
      plannedHomePosition: [0, 0, 0],
      vehicleType: 2,
      version: 2,
      items: waypoints.map((waypoint, index) => {
        const commandId = waypoint.commandIdOverride ?? qgcCommandIdForItemType(waypoint.itemType);
        const isDoJump = commandId === 177;
        const p1 = isDoJump ? waypoint.jumpTargetIndex : waypoint.holdTimeS;
        const p2 = isDoJump ? waypoint.jumpRepeatCount : waypoint.acceptanceRadiusM;
        const p3 = waypoint.param3;
        const p4 = waypoint.yawDeg ?? 0;
        return {
          AMSLAltAboveTerrain: null,
          Altitude: waypoint.altitude,
          AltitudeMode: 1,
          autoContinue: true,
          command: commandId,
          doJumpId: index + 1,
          frame: 3,
          params: [p1, p2, p3, p4, waypoint.latitude, waypoint.longitude, waypoint.altitude],
          type: "SimpleItem",
        };
      }),
      defaultAltitude,
    },
  };
}

function missionWaypointsToWpl(waypoints: MissionPlannerWaypoint[]): string {
  const lines = ["QGC WPL 110"];
  lines.push([0, 1, 0, 16, 0, 0, 0, 0, 0, 0, 0, 1].join("\t"));
  waypoints.forEach((waypoint, index) => {
    const commandId = waypoint.commandIdOverride ?? qgcCommandIdForItemType(waypoint.itemType);
    const isDoJump = commandId === 177;
    const p1 = isDoJump ? waypoint.jumpTargetIndex : waypoint.holdTimeS;
    const p2 = isDoJump ? waypoint.jumpRepeatCount : waypoint.acceptanceRadiusM;
    lines.push(
      [
        index + 1,
        index === 0 ? 1 : 0,
        3,
        commandId,
        p1,
        p2,
        waypoint.param3,
        waypoint.yawDeg ?? 0,
        waypoint.latitude,
        waypoint.longitude,
        waypoint.altitude,
        1,
      ].join("\t"),
    );
  });
  return lines.join("\n");
}

function parseQgcPlanWaypoints(raw: string): { waypoints: MissionPlannerWaypoint[]; defaultAltitude?: number } | null {
  try {
    const parsed = JSON.parse(raw) as { mission?: { items?: Array<{ command?: number; params?: number[] }>; defaultAltitude?: number } };
    const items = parsed.mission?.items ?? [];
    const waypoints = items
      .map((item): MissionPlannerWaypoint | null => {
        const params = Array.isArray(item.params) ? item.params : [];
        const commandId = Number(item.command ?? 16);
        const itemType = itemTypeForCommandId(commandId);
        const lat = Number(params[4]);
        const lon = Number(params[5]);
        const alt = Number(params[6]);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
          return null;
        }
        return {
          id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          latitude: lat,
          longitude: lon,
          altitude: Number.isFinite(alt) ? alt : 30,
          itemType,
          commandIdOverride: commandId,
          param3: Number(params[2] ?? 0),
          jumpTargetIndex: Number(params[0] ?? 1),
          jumpRepeatCount: Number(params[1] ?? 1),
          holdTimeS: Number(params[0] ?? 0),
          acceptanceRadiusM: Number(params[1] ?? 8),
          yawDeg: Number.isFinite(Number(params[3])) ? Number(params[3]) : null,
        };
      })
      .filter((waypoint): waypoint is MissionPlannerWaypoint => waypoint !== null);
    return { waypoints, defaultAltitude: parsed.mission?.defaultAltitude };
  } catch {
    return null;
  }
}

function parseWplWaypoints(raw: string): MissionPlannerWaypoint[] {
  const lines = raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith("#"));
  if (lines.length === 0 || !lines[0].toUpperCase().startsWith("QGC WPL")) {
    return [];
  }
  const waypoints: MissionPlannerWaypoint[] = [];
  for (const line of lines.slice(1)) {
    const parts = line.split(/\s+/);
    if (parts.length < 12) {
      continue;
    }
    const seq = Number(parts[0]);
    if (!Number.isFinite(seq) || seq === 0) {
      continue;
    }
    const commandId = Number(parts[3]);
    const p1 = Number(parts[4]);
    const p2 = Number(parts[5]);
    const p3 = Number(parts[6]);
    const p4 = Number(parts[7]);
    const lat = Number(parts[8]);
    const lon = Number(parts[9]);
    const alt = Number(parts[10]);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      continue;
    }
    waypoints.push({
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      latitude: lat,
      longitude: lon,
      altitude: Number.isFinite(alt) ? alt : 30,
      itemType: itemTypeForCommandId(commandId),
      commandIdOverride: Number.isFinite(commandId) ? commandId : null,
      param3: Number.isFinite(p3) ? p3 : 0,
      jumpTargetIndex: Number.isFinite(p1) ? p1 : 1,
      jumpRepeatCount: Number.isFinite(p2) ? p2 : 1,
      holdTimeS: Number.isFinite(p1) ? p1 : 0,
      acceptanceRadiusM: Number.isFinite(p2) ? p2 : 8,
      yawDeg: Number.isFinite(p4) ? p4 : null,
    });
  }
  return waypoints;
}

function MissionPlannerMode({
  center,
  zoom,
  onZoomChange,
  onCenterChange,
  mapLayer,
  vehicles,
  missionPlans,
  canCommandVehicle,
  onCommand,
}: {
  center: [number, number];
  zoom: number;
  onZoomChange: (zoom: number) => void;
  onCenterChange: (center: [number, number]) => void;
  mapLayer: { url: string; attribution: string; maxNativeZoom: number };
  vehicles: Vehicle[];
  missionPlans: Record<string, [number, number][]>;
  canCommandVehicle: (vehicleId: string) => boolean;
  onCommand: (vehicleId: string, cmd: Command) => void;
}) {
  const commandableVehicles = useMemo(
    () => vehicles.filter((vehicle) => vehicle.vehicle_type !== "yp" && canCommandVehicle(vehicle.vehicle_id)),
    [vehicles, canCommandVehicle],
  );
  const [waypoints, setWaypoints] = useState<MissionPlannerWaypoint[]>([]);
  const [selectedVehicleId, setSelectedVehicleId] = useState<string>("");
  const [editingWaypointId, setEditingWaypointId] = useState<string | null>(null);
  const [defaultWaypointAltitude, setDefaultWaypointAltitude] = useState<number>(30);
  const [forceGuidedOnComplete, setForceGuidedOnComplete] = useState<boolean>(false);
  const missionFileInputRef = useRef<HTMLInputElement | null>(null);
  // Suppress one map-click add after a marker drag completes.
  const markerDragRef = useRef(false);
  const [plannerFrame, setPlannerFrame] = useState(() => {
    const topSafe = 132;
    const bottomSafe = 96;
    const minHeight = 320;
    const maxAvailable = Math.max(minHeight, window.innerHeight - topSafe - bottomSafe);
    return {
      x: 16,
      y: topSafe,
      height: maxAvailable,
    };
  });
  const plannerDragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    frameX: number;
    frameY: number;
  } | null>(null);
  const plannerResizeRef = useRef<{
    pointerId: number;
    startY: number;
    frameHeight: number;
  } | null>(null);
  const [editorFrame, setEditorFrame] = useState(() => ({
    x: Math.max(12, Math.round(window.innerWidth / 2) - 190),
    y: Math.max(12, Math.round(window.innerHeight / 2) - 190),
    width: 380,
  }));
  const editorDragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    frameX: number;
    frameY: number;
  } | null>(null);

  useEffect(() => {
    if (!commandableVehicles.find((vehicle) => vehicle.vehicle_id === selectedVehicleId)) {
      setSelectedVehicleId(commandableVehicles[0]?.vehicle_id ?? "");
    }
  }, [commandableVehicles, selectedVehicleId]);

  const addWaypoint = (lat: number, lon: number) => {
    setWaypoints((current) => [
      ...current,
      {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        latitude: lat,
        longitude: lon,
        altitude: defaultWaypointAltitude,
        itemType: "waypoint",
        commandIdOverride: null,
        param3: 0,
        jumpTargetIndex: 1,
        jumpRepeatCount: 1,
        holdTimeS: 0,
        acceptanceRadiusM: 8,
        yawDeg: null,
      },
    ]);
  };

  const updateWaypoint = (waypointId: string, updates: Partial<MissionPlannerWaypoint>) => {
    setWaypoints((current) => current.map((waypoint) => (waypoint.id === waypointId ? { ...waypoint, ...updates } : waypoint)));
  };

  const moveWaypoint = (waypointId: string, direction: -1 | 1) => {
    setWaypoints((current) => {
      const index = current.findIndex((waypoint) => waypoint.id === waypointId);
      if (index < 0) return current;
      const nextIndex = index + direction;
      if (nextIndex < 0 || nextIndex >= current.length) return current;
      const reordered = [...current];
      const [item] = reordered.splice(index, 1);
      reordered.splice(nextIndex, 0, item);
      return reordered;
    });
  };

  const removeWaypoint = (waypointId: string) => {
    setWaypoints((current) => current.filter((waypoint) => waypoint.id !== waypointId));
    if (editingWaypointId === waypointId) {
      setEditingWaypointId(null);
    }
  };

  const editingWaypoint = waypoints.find((waypoint) => waypoint.id === editingWaypointId) ?? null;

  const clampEditorFrame = (candidate: { x: number; y: number; width: number }) => {
    const width = Math.min(Math.max(320, candidate.width), Math.max(320, window.innerWidth - 24));
    const maxX = Math.max(12, window.innerWidth - width - 12);
    const maxY = Math.max(12, window.innerHeight - 260);
    return {
      width,
      x: Math.min(Math.max(12, candidate.x), maxX),
      y: Math.min(Math.max(12, candidate.y), maxY),
    };
  };

  const clampPlannerFrame = (candidate: { x: number; y: number; height: number }) => {
    const topSafe = 132;
    const bottomSafe = 96;
    const minHeight = 320;
    const panelWidth = Math.min(340, window.innerWidth - 32);
    const maxHeight = Math.max(minHeight, window.innerHeight - topSafe - bottomSafe);
    const height = Math.min(Math.max(minHeight, candidate.height), maxHeight);
    const maxX = Math.max(12, window.innerWidth - panelWidth - 12);
    const maxY = Math.max(topSafe, window.innerHeight - bottomSafe - height);
    return {
      x: Math.min(Math.max(12, candidate.x), maxX),
      y: Math.min(Math.max(topSafe, candidate.y), maxY),
      height,
    };
  };

  const dockPlannerLeft = () => {
    const topSafe = 132;
    const bottomSafe = 96;
    const minHeight = 320;
    const tallHeight = Math.max(minHeight, window.innerHeight - topSafe - bottomSafe);
    setPlannerFrame(clampPlannerFrame({ x: 16, y: topSafe, height: tallHeight }));
  };

  const startEditorDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    editorDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      frameX: editorFrame.x,
      frameY: editorFrame.y,
    };
  };

  const moveEditorDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = editorDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    setEditorFrame((current) => clampEditorFrame({ ...current, x: drag.frameX + dx, y: drag.frameY + dy }));
  };

  const endEditorDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (editorDragRef.current?.pointerId === event.pointerId) {
      editorDragRef.current = null;
    }
  };

  const startPlannerDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    plannerDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      frameX: plannerFrame.x,
      frameY: plannerFrame.y,
    };
  };

  const movePlannerDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = plannerDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    setPlannerFrame((current) => clampPlannerFrame({ ...current, x: drag.frameX + dx, y: drag.frameY + dy }));
  };

  const endPlannerDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (plannerDragRef.current?.pointerId === event.pointerId) {
      plannerDragRef.current = null;
    }
  };

  const startPlannerResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    plannerResizeRef.current = {
      pointerId: event.pointerId,
      startY: event.clientY,
      frameHeight: plannerFrame.height,
    };
  };

  const movePlannerResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = plannerResizeRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dy = event.clientY - drag.startY;
    setPlannerFrame((current) => clampPlannerFrame({ ...current, height: drag.frameHeight + dy }));
  };

  const endPlannerResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (plannerResizeRef.current?.pointerId === event.pointerId) {
      plannerResizeRef.current = null;
    }
  };

  useEffect(() => {
    const onResize = () => {
      setPlannerFrame((current) => clampPlannerFrame(current));
      setEditorFrame((current) => clampEditorFrame(current));
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const saveMissionToFile = () => {
    if (waypoints.length === 0) {
      alert("Add at least one waypoint before saving a mission file.");
      return;
    }
    const payload = {
      version: 1,
      saved_at: new Date().toISOString(),
      default_altitude_m: defaultWaypointAltitude,
      force_guided_on_complete: forceGuidedOnComplete,
      waypoints: waypoints.map((waypoint) => ({
        latitude: waypoint.latitude,
        longitude: waypoint.longitude,
        altitude: waypoint.altitude,
        item_type: waypoint.itemType,
        command_id: waypoint.commandIdOverride,
        param1: waypoint.itemType === "do_jump" ? waypoint.jumpTargetIndex : waypoint.holdTimeS,
        param2: waypoint.itemType === "do_jump" ? waypoint.jumpRepeatCount : waypoint.acceptanceRadiusM,
        param3: waypoint.param3,
        param4: waypoint.yawDeg,
        hold_time_s: waypoint.holdTimeS,
        acceptance_radius_m: waypoint.acceptanceRadiusM,
        yaw_deg: waypoint.yawDeg,
      })),
    };
    downloadTextFile(
      `mission-${new Date().toISOString().replace(/[:.]/g, "-")}.json`,
      JSON.stringify(payload, null, 2),
      "application/json",
    );
  };

  const saveQgcPlanFile = () => {
    if (waypoints.length === 0) {
      alert("Add at least one waypoint before exporting a QGroundControl plan.");
      return;
    }
    const qgcPlan = missionWaypointsToQgcPlan(waypoints, defaultWaypointAltitude);
    downloadTextFile(
      `mission-${new Date().toISOString().replace(/[:.]/g, "-")}.plan`,
      JSON.stringify(qgcPlan, null, 2),
      "application/json",
    );
  };

  const saveWplFile = () => {
    if (waypoints.length === 0) {
      alert("Add at least one waypoint before exporting a Mission Planner WPL file.");
      return;
    }
    downloadTextFile(
      `mission-${new Date().toISOString().replace(/[:.]/g, "-")}.waypoints`,
      missionWaypointsToWpl(waypoints),
      "text/plain",
    );
  };

  const loadMissionFromFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    try {
      const text = await file.text();
      const qgcPlan = parseQgcPlanWaypoints(text);
      let loadedWaypoints: MissionPlannerWaypoint[] = [];
      let loadedDefaultAltitude: number | undefined;

      if (qgcPlan && qgcPlan.waypoints.length > 0) {
        loadedWaypoints = qgcPlan.waypoints;
        loadedDefaultAltitude = qgcPlan.defaultAltitude;
      } else {
        const wplWaypoints = parseWplWaypoints(text);
        if (wplWaypoints.length > 0) {
          loadedWaypoints = wplWaypoints;
        } else {
          const parsed = JSON.parse(text) as {
            default_altitude_m?: number;
            force_guided_on_complete?: boolean;
            waypoints?: Array<{
              latitude?: number;
              longitude?: number;
              altitude?: number;
              item_type?: MissionPlannerWaypoint["itemType"];
              command_id?: number;
              param1?: number;
              param2?: number;
              param3?: number;
              param4?: number;
              hold_time_s?: number;
              acceptance_radius_m?: number;
              yaw_deg?: number | null;
            }>;
          };
          loadedDefaultAltitude = parsed.default_altitude_m;
          if (typeof parsed.force_guided_on_complete === "boolean") {
            setForceGuidedOnComplete(parsed.force_guided_on_complete);
          }
          loadedWaypoints = (parsed.waypoints ?? [])
            .filter((waypoint) => typeof waypoint.latitude === "number" && typeof waypoint.longitude === "number")
            .map((waypoint) => ({
              id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
              latitude: Number(waypoint.latitude),
              longitude: Number(waypoint.longitude),
              altitude: Number(waypoint.altitude ?? parsed.default_altitude_m ?? defaultWaypointAltitude),
              itemType: waypoint.item_type ?? "waypoint",
              commandIdOverride: waypoint.command_id == null ? null : Number(waypoint.command_id),
              param3: Number(waypoint.param3 ?? 0),
              jumpTargetIndex: Number(waypoint.param1 ?? 1),
              jumpRepeatCount: Number(waypoint.param2 ?? 1),
              holdTimeS: Number(waypoint.hold_time_s ?? waypoint.param1 ?? 0),
              acceptanceRadiusM: Number(waypoint.acceptance_radius_m ?? waypoint.param2 ?? 8),
              yawDeg: waypoint.yaw_deg == null
                ? (waypoint.param4 == null ? null : Number(waypoint.param4))
                : Number(waypoint.yaw_deg),
            } satisfies MissionPlannerWaypoint));
        }
      }

      if (loadedWaypoints.length === 0) {
        alert("Mission file did not contain valid waypoints.");
        return;
      }

      setWaypoints(loadedWaypoints);
      setEditingWaypointId(null);
      if (typeof loadedDefaultAltitude === "number" && Number.isFinite(loadedDefaultAltitude)) {
        setDefaultWaypointAltitude(loadedDefaultAltitude);
      }
    } catch {
      alert("Failed to load mission file. Supported formats: native JSON, QGroundControl .plan, Mission Planner WPL.");
    } finally {
      event.target.value = "";
    }
  };

  const uploadMission = () => {
    if (!selectedVehicleId) {
      alert("Select a vehicle before uploading a mission.");
      return;
    }
    if (waypoints.length === 0) {
      alert("Add at least one waypoint before uploading.");
      return;
    }

    onCommand(selectedVehicleId, {
      type: "mission_plan",
      auto_arm_start: true,
      force_guided_on_complete: forceGuidedOnComplete,
      waypoints: waypoints.map((waypoint) => ({
        command_id: waypoint.commandIdOverride ?? qgcCommandIdForItemType(waypoint.itemType),
        latitude: waypoint.latitude,
        longitude: waypoint.longitude,
        altitude: waypoint.altitude,
        item_type: waypoint.itemType,
        param1: waypoint.itemType === "do_jump" ? waypoint.jumpTargetIndex : waypoint.holdTimeS,
        param2: waypoint.itemType === "do_jump" ? waypoint.jumpRepeatCount : waypoint.acceptanceRadiusM,
        param3: waypoint.param3,
        param4: waypoint.yawDeg ?? 0,
        hold_time_s: waypoint.holdTimeS,
        acceptance_radius_m: waypoint.acceptanceRadiusM,
        yaw_deg: waypoint.yawDeg,
      })),
    });

    alert(`Uploaded and started mission (${waypoints.length} waypoints) on ${selectedVehicleId}.`);
  };

  return (
    <div className="mission-planner-root">
      <MapContainer center={center} zoom={zoom} minZoom={3} maxZoom={20} zoomControl className="map">
        <TileLayer
          key={`mission-${mapLayer.url}`}
          url={mapLayer.url}
          attribution={mapLayer.attribution}
          maxNativeZoom={mapLayer.maxNativeZoom}
          maxZoom={20}
        />
        <MapZoomTracker onZoom={onZoomChange} />
        <MissionMapPanTracker onPan={onCenterChange} />
        <MissionPlannerClickCapture onAdd={addWaypoint} suppressAddRef={markerDragRef} />

        {vehicles.filter((vehicle) => vehicle.position).map((vehicle) => (
          <VehicleLayer
            key={`mission-${vehicle.vehicle_id}`}
            vehicle={vehicle}
            trailSeconds={30}
            isPhoneViewer={false}
            mapZoom={zoom}
            onClick={() => undefined}
          />
        ))}

        {waypoints.length > 1 && (
          <Polyline
            positions={waypoints.map((waypoint) => [waypoint.latitude, waypoint.longitude] as [number, number])}
            pathOptions={{ color: "#2563eb", weight: 3, opacity: 0.9 }}
          />
        )}

        {Object.entries(missionPlans).map(([vehicleId, missionWaypoints]) => (
          missionWaypoints.length > 1 && <Polyline key={`published-mission-${vehicleId}`} positions={missionWaypoints} pathOptions={{ color: "#16a34a", weight: 3, opacity: 0.9 }} />
        ))}

        {waypoints.map((waypoint, index) => (
          <MissionWaypointMarker
            key={waypoint.id}
            waypoint={waypoint}
            index={index}
            isSelected={waypoint.id === editingWaypointId}
            onSetEditing={setEditingWaypointId}
            onUpdate={updateWaypoint}
            markerDragRef={markerDragRef}
          />
        ))}
      </MapContainer>

      <div
        className="mission-planner-panel"
        style={{ left: plannerFrame.x, top: plannerFrame.y, height: plannerFrame.height }}
        onClick={(event) => event.stopPropagation()}
      >
        <div
          className="mission-planner-panel-title"
          onPointerDown={startPlannerDrag}
          onPointerMove={movePlannerDrag}
          onPointerUp={endPlannerDrag}
        >
          <strong>Mission Planner</strong>
          <button
            type="button"
            className="mission-dock-btn"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => {
              event.stopPropagation();
              dockPlannerLeft();
            }}
            title="Dock panel to left side"
          >
            Dock Left
          </button>
        </div>
        <div className="mission-planner-help">
          Left-click map to add waypoints. Drag points to move. Click a waypoint to edit details.
        </div>
        <label>
          Vehicle
          <select value={selectedVehicleId} onChange={(event) => setSelectedVehicleId(event.target.value)}>
            <option value="">-- Select vehicle --</option>
            {commandableVehicles.map((vehicle) => (
              <option key={vehicle.vehicle_id} value={vehicle.vehicle_id}>
                {vehicle.vehicle_id} ({vehicle.vehicle_type})
              </option>
            ))}
          </select>
        </label>
        <label>
          Default waypoint altitude (m)
          <input
            type="number"
            value={defaultWaypointAltitude}
            onChange={(event) => setDefaultWaypointAltitude(Number(event.target.value) || 0)}
          />
        </label>
        <label className="setting-toggle mission-guided-toggle">
          <span>Force GUIDED after mission completion</span>
          <input
            type="checkbox"
            checked={forceGuidedOnComplete}
            onChange={(event) => setForceGuidedOnComplete(event.target.checked)}
          />
        </label>
        <div className="mission-planner-summary">Waypoints: {waypoints.length}</div>
        <div className="mission-waypoint-table">
          <div className="mission-waypoint-table-header">Seq</div>
          <div className="mission-waypoint-table-header">Type</div>
          <div className="mission-waypoint-table-header">Alt</div>
          <div className="mission-waypoint-table-header">Actions</div>
          {waypoints.map((waypoint, index) => (
            <div className="mission-waypoint-row" key={waypoint.id}>
              <div className="mission-waypoint-cell">{index + 1}</div>
              <div className="mission-waypoint-cell">{waypoint.itemType}</div>
              <div className="mission-waypoint-cell">{waypoint.altitude.toFixed(0)} m</div>
              <div className="mission-waypoint-cell mission-waypoint-actions-cell">
                <button type="button" onClick={() => moveWaypoint(waypoint.id, -1)} disabled={index === 0}>↑</button>
                <button type="button" onClick={() => moveWaypoint(waypoint.id, 1)} disabled={index === waypoints.length - 1}>↓</button>
                <button type="button" onClick={() => setEditingWaypointId(waypoint.id)}>Edit</button>
              </div>
            </div>
          ))}
        </div>
        <div className="mission-planner-actions">
          <button type="button" onClick={() => setWaypoints((current) => current.slice(0, -1))} disabled={waypoints.length === 0}>
            Remove Last
          </button>
          <button type="button" onClick={() => { setWaypoints([]); setEditingWaypointId(null); }} disabled={waypoints.length === 0}>
            Clear Mission
          </button>
          <button type="button" onClick={saveMissionToFile} disabled={waypoints.length === 0}>
            Save JSON
          </button>
          <button type="button" onClick={saveQgcPlanFile} disabled={waypoints.length === 0}>
            Export QGC .plan
          </button>
          <button type="button" onClick={saveWplFile} disabled={waypoints.length === 0}>
            Export WPL
          </button>
          <button type="button" onClick={() => missionFileInputRef.current?.click()}>
            Import Mission
          </button>
          <button type="button" className="mission-upload" onClick={uploadMission} disabled={!selectedVehicleId || waypoints.length === 0}>
            Upload + Arm + Start
          </button>
        </div>
        <input
          ref={missionFileInputRef}
          type="file"
          accept="application/json,.json,.plan,.waypoints,.txt"
          style={{ display: "none" }}
          onChange={loadMissionFromFile}
        />
        <div
          className="mission-planner-resize-handle"
          onPointerDown={startPlannerResize}
          onPointerMove={movePlannerResize}
          onPointerUp={endPlannerResize}
          title="Resize planner height"
        />
      </div>

      {editingWaypoint && (
        <div className="mission-waypoint-modal-overlay" onClick={() => setEditingWaypointId(null)}>
          <div
            className="mission-waypoint-modal"
            style={{ left: editorFrame.x, top: editorFrame.y, width: editorFrame.width, position: "fixed" }}
            onClick={(event) => event.stopPropagation()}
          >
            <div
              className="mission-waypoint-modal-title mission-waypoint-modal-drag-handle"
              onPointerDown={startEditorDrag}
              onPointerMove={moveEditorDrag}
              onPointerUp={endEditorDrag}
            >
              Waypoint {waypoints.findIndex((wp) => wp.id === editingWaypoint.id) + 1}
            </div>
            <label>
              Item Type
              <select
                value={editingWaypoint.itemType}
                onChange={(event) =>
                  updateWaypoint(editingWaypoint.id, {
                    itemType: event.target.value as MissionPlannerWaypoint["itemType"],
                    commandIdOverride: null,
                  })
                }
              >
                <option value="waypoint">Waypoint</option>
                <option value="takeoff">Takeoff</option>
                <option value="loiter_time">Loiter Time</option>
                <option value="land">Land</option>
                <option value="rtl">Return To Launch</option>
                <option value="do_jump">Conditional Jump (DO_JUMP)</option>
              </select>
            </label>
            <label>
              MAV_CMD Override (optional)
              <input
                type="number"
                value={editingWaypoint.commandIdOverride ?? ""}
                placeholder={`${qgcCommandIdForItemType(editingWaypoint.itemType)}`}
                onChange={(event) => {
                  const value = event.target.value.trim();
                  updateWaypoint(editingWaypoint.id, { commandIdOverride: value === "" ? null : Number(value) });
                }}
              />
            </label>
            <label>
              Altitude (m)
              <input
                type="number"
                value={editingWaypoint.altitude}
                onChange={(event) => updateWaypoint(editingWaypoint.id, { altitude: Number(event.target.value) })}
              />
            </label>
            {editingWaypoint.itemType === "do_jump" ? (
              <>
                <label>
                  Jump Target Waypoint #
                  <input
                    type="number"
                    min={1}
                    max={Math.max(1, waypoints.length)}
                    value={editingWaypoint.jumpTargetIndex}
                    onChange={(event) =>
                      updateWaypoint(editingWaypoint.id, {
                        jumpTargetIndex: Math.min(Math.max(1, Number(event.target.value)), Math.max(1, waypoints.length)),
                      })
                    }
                  />
                </label>
                <label>
                  Jump Repeat Count
                  <input
                    type="number"
                    min={1}
                    value={editingWaypoint.jumpRepeatCount}
                    onChange={(event) => updateWaypoint(editingWaypoint.id, { jumpRepeatCount: Math.max(1, Number(event.target.value)) })}
                  />
                </label>
              </>
            ) : (
              <>
                <label>
                  Hold Time (s)
                  <input
                    type="number"
                    min={0}
                    value={editingWaypoint.holdTimeS}
                    onChange={(event) => updateWaypoint(editingWaypoint.id, { holdTimeS: Math.max(0, Number(event.target.value)) })}
                  />
                </label>
                <label>
                  Acceptance Radius (m)
                  <input
                    type="number"
                    min={1}
                    value={editingWaypoint.acceptanceRadiusM}
                    onChange={(event) => updateWaypoint(editingWaypoint.id, { acceptanceRadiusM: Math.max(1, Number(event.target.value)) })}
                  />
                </label>
              </>
            )}
            <label>
              Param3
              <input
                type="number"
                value={editingWaypoint.param3}
                onChange={(event) => updateWaypoint(editingWaypoint.id, { param3: Number(event.target.value) || 0 })}
              />
            </label>
            <label>
              Yaw (deg, optional)
              <input
                type="number"
                value={editingWaypoint.yawDeg ?? ""}
                placeholder="leave blank"
                onChange={(event) => {
                  const value = event.target.value.trim();
                  updateWaypoint(editingWaypoint.id, { yawDeg: value === "" ? null : Number(value) });
                }}
              />
            </label>
            <div className="mission-waypoint-modal-actions">
              <button type="button" onClick={() => removeWaypoint(editingWaypoint.id)} className="danger">
                Delete Waypoint
              </button>
              <button type="button" onClick={() => setEditingWaypointId(null)}>
                Done
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function MissionWaypointMarker({
  waypoint,
  index,
  isSelected,
  onSetEditing,
  onUpdate,
  markerDragRef,
}: {
  waypoint: MissionPlannerWaypoint;
  index: number;
  isSelected: boolean;
  onSetEditing: (id: string) => void;
  onUpdate: (id: string, updates: Partial<MissionPlannerWaypoint>) => void;
  markerDragRef: { current: boolean };
}) {
  const icon = useMemo(() => missionWaypointIcon(index + 1, isSelected), [index, isSelected]);
  const position = useMemo<[number, number]>(() => [waypoint.latitude, waypoint.longitude], [waypoint.latitude, waypoint.longitude]);

  return (
    <Marker
      position={position}
      icon={icon}
      draggable
      zIndexOffset={7000 + index}
      eventHandlers={{
        click: (event) => {
          L.DomEvent.stopPropagation(event.originalEvent);
          onSetEditing(waypoint.id);
        },
        dragstart: () => {
          markerDragRef.current = true;
        },
        dragend: (event) => {
          markerDragRef.current = false;
          const pos = event.target.getLatLng();
          onUpdate(waypoint.id, { latitude: pos.lat, longitude: pos.lng });
        },
      }}
    />
  );
}

function MissionPlannerClickCapture({
  onAdd,
  suppressAddRef,
}: {
  onAdd: (lat: number, lon: number) => void;
  suppressAddRef: { current: boolean };
}) {
  useMapEvents({
    click(event) {
      if (suppressAddRef.current) {
        suppressAddRef.current = false;
        return;
      }
      onAdd(event.latlng.lat, event.latlng.lng);
    },
  });
  return null;
}

function missionWaypointIcon(index: number, selected: boolean): L.DivIcon {
  return L.divIcon({
    className: "",
    iconSize: [28, 28],
    iconAnchor: [14, 14],
    html: `<div class="mission-waypoint-dot${selected ? " selected" : ""}">${index}</div>`,
  });
}

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
    if (waypoints.length === 0) {
      alert("Add at least one waypoint before dispatching.");
      return;
    }
    if (!selectedVehicleId) {
      alert("Please select a vehicle to dispatch.");
      return;
    }

    const localWaypoints: RelativeWaypoint[] = waypoints.map(({ x, y, z }) => ({ x, y, z }));

    onCommand(selectedVehicleId, {
      type: "ship_relative_trajectory",
      ship_vehicle_id: yp.vehicle_id,
      local_waypoints: localWaypoints,
      arrival_radius_m: 6,
      update_hz: 10,
    });

    alert(`Dispatched ${localWaypoints.length} ship-relative waypoints to ${selectedVehicleId}`);
    setWaypoints([]);
    setSelectedId(null);
  };

  // Keep waypoint mission math unchanged, but mirror scene X to match the ship model's lateral orientation.
  const linePoints = waypoints.map((wp) => [-wp.x, wp.z, wp.y] as [number, number, number]);

  return (
    <div className="planner-container" style={{ display: "flex", flexDirection: "column", width: "100%", height: "100%", overflow: "hidden", backgroundColor: "#0f172a", color: "white", paddingTop: 60 }}>
      
      {/* TOP PANEL: 3D Render Area */}
      <div style={{ flex: "2 1 0", minHeight: 0, position: 'relative', borderBottom: '2px solid #334155', overflow: 'hidden' }}>
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
            <Sphere key={wp.id} position={[-wp.x, wp.z, wp.y]} args={[1.5, 16, 16]}>
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
      <div style={{ flex: "3 1 0", minHeight: 0, display: "flex", overflow: "hidden" }}>
        
        {/* BOTTOM LEFT: 2D Top-Down View */}
        <div style={{ flex: "1 1 0", minWidth: 0, padding: 20, borderRight: '2px solid #334155', display: "flex", flexDirection: "column", overflow: "hidden" }}>
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
          
          <div style={{ flex: 1, minHeight: 0, display: "flex", justifyContent: "center", alignItems: "center", overflow: "hidden" }}>
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
        <div style={{ flex: "1 1 0", minWidth: 0, padding: 20, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          <h2 style={{ fontSize: "1.2rem", fontWeight: "bold", marginBottom: 10 }}>Altitude Profile</h2>
          
          <div style={{ flex: 1, minHeight: 0, backgroundColor: "#1e293b", borderRadius: 8, border: "1px solid #475569", position: "relative", marginBottom: 15, padding: "10px 0", overflow: "hidden" }}>
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

function tileLayerFor(base: MapBase, source: MapSource): { url: string; attribution: string; maxNativeZoom: number } {
  if (base === "street") {
    return {
      auto: {
        url: "/tiles/osm/{z}/{x}/{y}.png",
        attribution: "&copy; OpenStreetMap contributors",
        maxNativeZoom: 19,
      },
      cache: {
        url: "/tiles/cache/{z}/{x}/{y}.png",
        attribution: "&copy; OpenStreetMap contributors",
        maxNativeZoom: 19,
      },
      online: {
        url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        attribution: "&copy; OpenStreetMap contributors",
        maxNativeZoom: 19,
      },
    }[source];
  }

  // Satellite: cached tiles only go to z=19 so we overzoom from there;
  // online Esri World Imagery natively serves z=20 in high-detail areas.
  return {
    auto: {
      url: "/tiles/earth/{z}/{x}/{y}.png",
      attribution: "Tiles &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community",
      maxNativeZoom: 19,
    },
    cache: {
      url: "/tiles/earth-cache/{z}/{x}/{y}.png",
      attribution: "Tiles &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community",
      maxNativeZoom: 19,
    },
    online: {
      url: "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      attribution: "Tiles &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community",
      maxNativeZoom: 20,
    },
  }[source];
}

function WeatherRadarLayer() {
  const [radarCacheBucket, setRadarCacheBucket] = useState(() => Math.floor(Date.now() / WEATHER_RADAR_REFRESH_MS));

  useEffect(() => {
    const interval = window.setInterval(() => setRadarCacheBucket(Math.floor(Date.now() / WEATHER_RADAR_REFRESH_MS)), WEATHER_RADAR_REFRESH_MS);
    return () => window.clearInterval(interval);
  }, []);

  return (
    <WMSTileLayer
      key={`weather-radar-${radarCacheBucket}`}
      url={`${WEATHER_RADAR_WMS_URL}?radar_cache=${radarCacheBucket}`}
      layers="1"
      format="image/png"
      transparent
      opacity={WEATHER_RADAR_OPACITY}
      attribution="Radar &copy; NOAA/NWS"
      errorTileUrl={TRANSPARENT_TILE_DATA_URL}
      eventHandlers={{
        tileerror: (event) => {
          const tile = (event as unknown as { tile?: HTMLImageElement }).tile;
          if (tile) tile.src = TRANSPARENT_TILE_DATA_URL;
        },
      }}
    />
  );
}

function WindLayer({ yp, showVectors, onToggleVectors }: { yp?: Vehicle; showVectors: boolean; onToggleVectors: () => void }) {
  const map = useMap();
  const [windState, setWindState] = useState<WindState>({ samples: [], projectedSamples: [] });
  const sampleKeyRef = useRef("");
  const samplesRef = useRef<WindSample[]>([]);

  useEffect(() => {
    if (!showVectors) return;
    map.attributionControl.addAttribution(WIND_OVERLAY_ATTRIBUTION);
    return () => {
      map.attributionControl.removeAttribution(WIND_OVERLAY_ATTRIBUTION);
    };
  }, [map, showVectors]);

  useEffect(() => { samplesRef.current = windState.samples; }, [windState.samples]);

  useEffect(() => {
    let cancelled = false;
    let controller: AbortController | null = null;
    const refreshSamples = () => {
      const points = windSamplePoints(map);
      const bucket = Math.floor(Date.now() / WIND_OVERLAY_REFRESH_MS);
      const nextKey = `${bucket}:${points.map((point) => `${point.latitude.toFixed(2)},${point.longitude.toFixed(2)}`).join("|")}`;
      if (nextKey === sampleKeyRef.current) {
        setWindState((current) => ({ ...current, projectedSamples: projectWindSamples(map, samplesRef.current) }));
        return;
      }
      sampleKeyRef.current = nextKey;
      controller?.abort();
      controller = new AbortController();
      fetchWindSamples(points, controller.signal).then((nextSamples) => {
        if (!cancelled) {
          samplesRef.current = nextSamples;
          setWindState({ samples: nextSamples, projectedSamples: projectWindSamples(map, nextSamples) });
        }
      });
    };
    const updateProjection = () => setWindState((current) => ({ ...current, projectedSamples: projectWindSamples(map, samplesRef.current) }));
    refreshSamples();
    map.on("moveend zoomend resize", refreshSamples);
    map.on("move zoom", updateProjection);
    const interval = window.setInterval(refreshSamples, WIND_OVERLAY_REFRESH_MS);
    return () => {
      cancelled = true;
      controller?.abort();
      window.clearInterval(interval);
      map.off("moveend zoomend resize", refreshSamples);
      map.off("move zoom", updateProjection);
    };
  }, [map]);

  const windReadout = windState.samples.length > 0 ? representativeWindSample(windState.samples) : null;
  const ypReadout = readoutForYp(yp);
  if (!windReadout && !ypReadout) return null;

  return createPortal(
    <>
      {showVectors && windState.projectedSamples.length > 0 && (
        <div className="wind-overlay" aria-hidden="true">
          <svg width="100%" height="100%" focusable="false">
            {windState.projectedSamples.map((sample) => (
              <g key={sample.id} transform={`translate(${sample.x.toFixed(1)} ${sample.y.toFixed(1)}) rotate(${windFlowDirection(sample.directionDeg)})`}>
                <line className="wind-arrow-line" x1="0" y1="13" x2="0" y2={windArrowTipY(sample.speedKmh)} />
                <path className="wind-arrow-head" d={`M -5 ${windArrowTipY(sample.speedKmh) + 7} L 0 ${windArrowTipY(sample.speedKmh)} L 5 ${windArrowTipY(sample.speedKmh) + 7}`} />
                <circle className="wind-arrow-dot" cx="0" cy="13" r="2.4" />
              </g>
            ))}
          </svg>
        </div>
      )}
      <button className="wind-readout" type="button" title="Toggle wind vectors" onClick={onToggleVectors}>
        {ypReadout && <ReadoutRow label="YP" heading={formatHeading(ypReadout.headingDeg)} speed={formatKnots(ypReadout.speedKts)} />}
        {windReadout && <ReadoutRow label="Wind" heading={formatHeading(windReadout.directionDeg)} speed={formatKnots(kmhToKnots(windReadout.speedKmh))} />}
      </button>
    </>,
    map.getContainer(),
  );
}

function ReadoutRow({ label, heading, speed }: { label: string; heading: string; speed: string }) {
  return <span className="readout-row"><span className="readout-label">{label}:</span><span className="readout-heading">{heading}</span><span className="readout-at">@</span><span className="readout-speed">{speed} kts</span></span>;
}

function windSamplePoints(map: L.Map): Array<{ latitude: number; longitude: number }> {
  const size = map.getSize();
  const points: Array<{ latitude: number; longitude: number }> = [];
  for (let row = 0; row < WIND_SAMPLE_ROWS; row += 1) {
    for (let column = 0; column < WIND_SAMPLE_COLUMNS; column += 1) {
      const latLng = map.containerPointToLatLng([(column + 0.5) / WIND_SAMPLE_COLUMNS * size.x, (row + 0.5) / WIND_SAMPLE_ROWS * size.y]);
      points.push({ latitude: latLng.lat, longitude: latLng.lng });
    }
  }
  return points;
}

function projectWindSamples(map: L.Map, samples: WindSample[]): ProjectedWindSample[] {
  return samples.map((sample) => {
    const point = map.latLngToContainerPoint([sample.latitude, sample.longitude]);
    return { ...sample, x: point.x, y: point.y };
  });
}

async function fetchWindSamples(points: Array<{ latitude: number; longitude: number }>, signal: AbortSignal): Promise<WindSample[]> {
  const results = await Promise.allSettled(points.map((point, index) => fetchWindSample(point.latitude, point.longitude, index, signal)));
  return results.flatMap((result) => (result.status === "fulfilled" && result.value ? [result.value] : []));
}

async function fetchWindSample(latitude: number, longitude: number, index: number, signal: AbortSignal): Promise<WindSample | null> {
  const timeoutController = new AbortController();
  const timeout = window.setTimeout(() => timeoutController.abort(), WIND_FETCH_TIMEOUT_MS);
  const abortListener = () => timeoutController.abort();
  signal.addEventListener("abort", abortListener, { once: true });
  try {
    const params = new URLSearchParams({ latitude: latitude.toFixed(4), longitude: longitude.toFixed(4), current: "wind_speed_10m,wind_direction_10m", wind_speed_unit: "kmh", timezone: "UTC" });
    const response = await fetch(`https://api.open-meteo.com/v1/forecast?${params.toString()}`, { signal: timeoutController.signal });
    if (!response.ok) return null;
    const payload = (await response.json()) as { current?: { wind_speed_10m?: number; wind_direction_10m?: number } };
    const speedKmh = Number(payload.current?.wind_speed_10m);
    const directionDeg = Number(payload.current?.wind_direction_10m);
    if (!Number.isFinite(speedKmh) || !Number.isFinite(directionDeg)) return null;
    return { id: `${index}-${latitude.toFixed(3)}-${longitude.toFixed(3)}`, latitude, longitude, speedKmh, directionDeg };
  } catch {
    return null;
  } finally {
    window.clearTimeout(timeout);
    signal.removeEventListener("abort", abortListener);
  }
}

function windArrowTipY(speedKmh: number): number { return -Math.min(30, Math.max(14, 11 + speedKmh * 0.55)); }
function windFlowDirection(directionDeg: number): number { return (directionDeg + 180) % 360; }
function representativeWindSample(samples: WindSample[]): WindSample { return samples[Math.floor(samples.length / 2)] ?? samples[0]; }
function kmhToKnots(speedKmh: number): number { return speedKmh * 0.539957; }
function readoutForYp(yp?: Vehicle): YpReadout | null {
  if (!yp) return null;
  const headingDeg = Number.isFinite(yp.heading) ? yp.heading : undefined;
  const speedKts = speedKnotsFromHistory(yp.history);
  if (headingDeg == null && speedKts == null) return null;
  return { headingDeg, speedKts };
}
function speedKnotsFromHistory(history?: Vehicle["history"]): number | undefined {
  if (!history || history.length < 2) return undefined;
  const recent = [...history].reverse();
  const latest = recent.find((point) => point.stamp != null);
  const previous = latest ? recent.find((point) => point !== latest && point.stamp != null && latest.stamp! - point.stamp! > 0.1) : undefined;
  if (!latest || !previous || latest.stamp == null || previous.stamp == null) return undefined;
  const elapsedSeconds = latest.stamp - previous.stamp;
  if (elapsedSeconds <= 0) return undefined;
  return metersPerSecondToKnots(haversineMeters(previous.latitude, previous.longitude, latest.latitude, latest.longitude) / elapsedSeconds);
}
function metersPerSecondToKnots(speedMps: number): number { return speedMps * 1.943844; }
function formatHeading(directionDeg?: number): string {
  if (typeof directionDeg !== "number" || !Number.isFinite(directionDeg)) return "---";
  return String(((Math.round(directionDeg) % 360) + 360) % 360).padStart(3, "0");
}
function formatKnots(speedKts?: number): string { return typeof speedKts === "number" && Number.isFinite(speedKts) ? String(Math.round(speedKts)) : "--"; }

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
  const [tab, setTab] = useState<"network" | "radio">("network");

  // --- Network tab state ---
  const [url, setUrl] = useState("");
  const [netVehicleId, setNetVehicleId] = useState("");
  const [netCameraHost, setNetCameraHost] = useState("");
  const [netError, setNetError] = useState<string | null>(null);
  const [netConnecting, setNetConnecting] = useState(false);

  // --- Radio tab state ---
  const [serialPorts, setSerialPorts] = useState<SerialPortInfo[]>([]);
  const [portsLoading, setPortsLoading] = useState(false);
  const [portsLoaded, setPortsLoaded] = useState(false);
  const [selectedPort, setSelectedPort] = useState("");
  const [manualPort, setManualPort] = useState("");
  const [baud, setBaud] = useState("57600");
  const [radioVehicleId, setRadioVehicleId] = useState("");
  const [radioCameraHost, setRadioCameraHost] = useState("");
  const [radioError, setRadioError] = useState<string | null>(null);
  const [radioConnecting, setRadioConnecting] = useState(false);

  // Windows relay helper state
  const [relayComPort, setRelayComPort] = useState("COM12");
  const [relayTcpPort, setRelayTcpPort] = useState("5762");
  const [relayCopied, setRelayCopied] = useState(false);

  const refreshPorts = () => {
    setPortsLoading(true);
    listSerialPorts()
      .then((ports) => {
        setSerialPorts(ports);
        setPortsLoaded(true);
        if (ports.length > 0 && !selectedPort) setSelectedPort(ports[0].device);
      })
      .finally(() => setPortsLoading(false));
  };

  // Auto-load ports when the radio tab is first opened
  const prevTab = useRef(tab);
  useEffect(() => {
    if (tab === "radio" && prevTab.current !== "radio") refreshPorts();
    prevTab.current = tab;
  }, [tab]);

  const handleNetConnect = async () => {
    const trimUrl = url.trim();
    if (!trimUrl) { setNetError("MAVLink URL is required"); return; }
    const validPrefixes = ["tcp:", "tcpin:", "tcpout:", "udpin:", "udpout:", "udpbcast:", "serial:"];
    if (!validPrefixes.some((p) => trimUrl.toLowerCase().startsWith(p))) {
      setNetError(`URL must start with: ${validPrefixes.join(", ")}`);
      return;
    }
    setNetError(null);
    setNetConnecting(true);
    const result = await connectSITL(trimUrl, netVehicleId.trim() || undefined, netCameraHost.trim() || undefined).catch((e) => ({ ok: false as const, error: String(e) }));
    setNetConnecting(false);
    if (!result.ok) { setNetError(result.error ?? "Connection failed"); }
    else { setUrl(""); setNetVehicleId(""); setNetCameraHost(""); }
  };

  const handleRadioConnect = async () => {
    const port = (selectedPort || manualPort).trim();
    if (!port) { setRadioError("Select or enter a serial port"); return; }
    const baudNum = parseInt(baud, 10);
    if (!baudNum || baudNum <= 0) { setRadioError("Invalid baud rate"); return; }
    const mavUrl = `serial:${port}:${baudNum}`;
    setRadioError(null);
    setRadioConnecting(true);
    const result = await connectSITL(mavUrl, radioVehicleId.trim() || undefined, radioCameraHost.trim() || undefined).catch((e) => ({ ok: false as const, error: String(e) }));
    setRadioConnecting(false);
    if (!result.ok) { setRadioError(result.error ?? "Connection failed"); }
    else { setRadioVehicleId(""); setRadioCameraHost(""); }
  };

  const bridgeList = Object.values(bridges);

  return (
    <div className="sitl-panel">
      <div className="panel-title">
        <Cable size={17} />
        <strong>Connections</strong>
      </div>

      <div className="sitl-tabs">
        <button
          className={tab === "network" ? "sitl-tab active" : "sitl-tab"}
          onClick={() => setTab("network")}
        >
          <Cable size={13} /> Network
        </button>
        <button
          className={tab === "radio" ? "sitl-tab active" : "sitl-tab"}
          onClick={() => setTab("radio")}
        >
          <Radio size={13} /> RFD-900
        </button>
      </div>

      {tab === "network" && (
        <div className="sitl-form">
          <label className="sitl-field-label">MAVLink URL</label>
          <input
            className="sitl-input"
            type="text"
            placeholder="tcp:localhost:5760"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !netConnecting && handleNetConnect()}
            spellCheck={false}
          />
          <label className="sitl-field-label">Vehicle ID <span className="sitl-optional">(optional)</span></label>
          <input
            className="sitl-input"
            type="text"
            placeholder="auto-generated from URL"
            value={netVehicleId}
            onChange={(e) => setNetVehicleId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !netConnecting && handleNetConnect()}
            spellCheck={false}
          />
          <label className="sitl-field-label">Camera Host <span className="sitl-optional">(optional)</span></label>
          <input
            className="sitl-input"
            type="text"
            placeholder="defaults to MAVLink URL host, if any"
            value={netCameraHost}
            onChange={(e) => setNetCameraHost(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !netConnecting && handleNetConnect()}
            spellCheck={false}
          />
          {netError && <div className="sitl-error">{netError}</div>}
          <button className="sitl-connect-btn" onClick={handleNetConnect} disabled={netConnecting}>
            {netConnecting ? <Loader2 size={15} className="sitl-spin" /> : <Plus size={15} />}
            {netConnecting ? "Connecting…" : "Connect"}
          </button>
          <div className="sitl-hint">
            Examples: <code>tcp:localhost:5760</code> · <code>udpin:0.0.0.0:14551</code>
          </div>
        </div>
      )}

      {tab === "radio" && (
        <div className="sitl-form">
          {/* Windows / Docker COM port limitation helper */}
          {portsLoaded && !portsLoading && serialPorts.length === 0 && (
            <div className="sitl-windows-hint">
              <strong>No serial ports visible to Docker</strong>
              <p>
                Docker Desktop on Windows cannot access COM ports directly.
                Run this relay script on your Windows machine to bridge the COM port over TCP:
              </p>
              <div className="sitl-relay-inputs">
                <input
                  className="sitl-input sitl-relay-field"
                  value={relayComPort}
                  onChange={(e) => setRelayComPort(e.target.value)}
                  spellCheck={false}
                  title="Windows COM port"
                  placeholder="COM12"
                />
                <input
                  className="sitl-input sitl-relay-field"
                  value={relayTcpPort}
                  onChange={(e) => setRelayTcpPort(e.target.value)}
                  spellCheck={false}
                  title="TCP port"
                  placeholder="5762"
                />
              </div>
              <code className="sitl-relay-cmd">
                python services/com_tcp_relay.py --port {relayComPort} --baud {baud} --tcp-port {relayTcpPort}
              </code>
              <div className="sitl-relay-actions">
                <button
                  className="sitl-relay-copy-btn"
                  onClick={() => {
                    navigator.clipboard.writeText(
                      `python services/com_tcp_relay.py --port ${relayComPort} --baud ${baud} --tcp-port ${relayTcpPort}`
                    );
                    setRelayCopied(true);
                    setTimeout(() => setRelayCopied(false), 2000);
                  }}
                >
                  {relayCopied ? "Copied!" : "Copy command"}
                </button>
                <button
                  className="sitl-relay-use-btn"
                  onClick={() => {
                    setUrl(`tcp:host.docker.internal:${relayTcpPort}`);
                    setTab("network");
                  }}
                >
                  Connect via Network tab →
                </button>
              </div>
            </div>
          )}

          <label className="sitl-field-label">
            Serial Port
            <button
              className="sitl-refresh-btn"
              title="Refresh port list"
              onClick={refreshPorts}
              disabled={portsLoading}
            >
              {portsLoading ? <Loader2 size={12} className="sitl-spin" /> : <RotateCcw size={12} />}
            </button>
          </label>
          {serialPorts.length > 0 ? (
            <select
              className="sitl-input"
              value={selectedPort}
              onChange={(e) => setSelectedPort(e.target.value)}
            >
              {serialPorts.map((p) => (
                <option key={p.device} value={p.device}>
                  {p.device}{p.description && p.description !== "n/a" ? ` — ${p.description}` : ""}
                </option>
              ))}
            </select>
          ) : (
            <input
              className="sitl-input"
              type="text"
              placeholder="/dev/ttyUSB0"
              value={manualPort}
              onChange={(e) => setManualPort(e.target.value)}
              spellCheck={false}
            />
          )}
          <label className="sitl-field-label">Baud Rate</label>
          <select
            className="sitl-input"
            value={baud}
            onChange={(e) => setBaud(e.target.value)}
          >
            <option value="57600">57600 (RFD-900 default)</option>
            <option value="115200">115200</option>
            <option value="9600">9600</option>
            <option value="38400">38400</option>
          </select>
          <label className="sitl-field-label">Vehicle ID <span className="sitl-optional">(optional)</span></label>
          <input
            className="sitl-input"
            type="text"
            placeholder="auto-generated"
            value={radioVehicleId}
            onChange={(e) => setRadioVehicleId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !radioConnecting && handleRadioConnect()}
            spellCheck={false}
          />
          <label className="sitl-field-label">Camera Host <span className="sitl-optional">(optional)</span></label>
          <input
            className="sitl-input"
            type="text"
            placeholder="e.g. 192.168.1.50"
            value={radioCameraHost}
            onChange={(e) => setRadioCameraHost(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !radioConnecting && handleRadioConnect()}
            spellCheck={false}
          />
          {radioError && <div className="sitl-error">{radioError}</div>}
          <button className="sitl-connect-btn" onClick={handleRadioConnect} disabled={radioConnecting}>
            {radioConnecting ? <Loader2 size={15} className="sitl-spin" /> : <Radio size={15} />}
            {radioConnecting ? "Connecting…" : "Connect Radio"}
          </button>
          <div className="sitl-hint">
            Requires the server container to have the USB device passed through via <code>devices:</code> in docker-compose (Linux hosts only).
          </div>
        </div>
      )}

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
        <div className="sitl-empty">No active connections.</div>
      )}
    </div>
  );
}

function MapMenu({
  mapBase,
  mapSource,
  showWeatherRadar,
  showWindOverlay,
  expanded,
  setMenuRef,
  setToggleRef,
  onExpandedChange,
  onMapBaseChange,
  onMapSourceChange,
  onWeatherRadarChange,
  onWindOverlayChange,
}: {
  mapBase: MapBase;
  mapSource: MapSource;
  showWeatherRadar: boolean;
  showWindOverlay: boolean;
  expanded: boolean;
  setMenuRef: (node: HTMLDivElement | null) => void;
  setToggleRef: (node: HTMLButtonElement | null) => void;
  onExpandedChange: (expanded: boolean) => void;
  onMapBaseChange: (base: MapBase) => void;
  onMapSourceChange: (source: MapSource) => void;
  onWeatherRadarChange: (show: boolean) => void;
  onWindOverlayChange: (show: boolean) => void;
}) {
  return (
    <div className="map-menu-shell" aria-label="Map options">
      <button ref={setToggleRef} className="map-menu-toggle" title="Map layers" onClick={() => onExpandedChange(!expanded)}>
        <Layers size={19} />
      </button>
      {expanded && (
        <div className="map-menu" ref={setMenuRef}>
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
          <fieldset>
            <legend>Overlay</legend>
            <label>
              <input type="checkbox" checked={showWeatherRadar} onChange={(event) => onWeatherRadarChange(event.target.checked)} />
              Weather radar
            </label>
            <label>
              <input type="checkbox" checked={showWindOverlay} onChange={(event) => onWindOverlayChange(event.target.checked)} />
              Winds
            </label>
          </fieldset>
        </div>
      )}
    </div>
  );
}

function gridSliderToMeters(v: number): number {
  // Maps slider 0–100 to 5–500 m on a log scale for finer control near 5 m
  const min = Math.log(5);
  const max = Math.log(500);
  return Math.round(Math.exp(min + (v / 100) * (max - min)));
}

function gridMetersToSlider(m: number): number {
  const min = Math.log(5);
  const max = Math.log(500);
  return Math.round(((Math.log(m) - min) / (max - min)) * 100);
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
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [showVehicles, setShowVehicles] = useState(false);
  const [showSearchGrid, setShowSearchGrid] = useState(false);
  const [gridSlider, setGridSlider] = useState(() => gridMetersToSlider(200));
  const gridSizeM = gridSliderToMeters(gridSlider);
  const [swathM, setSwathM] = useState(20);
  const [altM, setAltM] = useState(30);
  const [position, setPosition] = useState<{ left: number; top: number }>({ left: menu.x + 8, top: menu.y + 8 });
  const commandableVehicles = vehicles.filter((vehicle) => vehicle.vehicle_type !== "yp");
  const preferredVehicle = commandableVehicles.find((vehicle) => vehicle.vehicle_id === preferredVehicleId);

  useEffect(() => {
    const clampToViewport = () => {
      const panel = menuRef.current;
      if (!panel) {
        return;
      }
      const padding = 8;
      const offset = 8;
      const width = panel.offsetWidth;
      const height = panel.offsetHeight;
      const desiredLeft = menu.x + offset;
      const desiredTop = menu.y + offset;
      const maxLeft = Math.max(padding, window.innerWidth - width - padding);
      const maxTop = Math.max(padding, window.innerHeight - height - padding);

      setPosition({
        left: Math.min(Math.max(padding, desiredLeft), maxLeft),
        top: Math.min(Math.max(padding, desiredTop), maxTop),
      });
    };

    clampToViewport();
    window.addEventListener("resize", clampToViewport);
    return () => window.removeEventListener("resize", clampToViewport);
  }, [menu.x, menu.y, showVehicles, showSearchGrid, gridSlider, swathM, altM, commandableVehicles.length, preferredVehicleId]);

  return (
    <div ref={menuRef} className="map-action-menu" style={{ left: position.left, top: position.top }} onClick={(event) => event.stopPropagation()}>
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
              type="range" min={0} max={100} step={1}
              value={gridSlider}
              onChange={(e) => setGridSlider(Number(e.target.value))}
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
          mousedown: (event) => {
            L.DomEvent.stopPropagation(event.originalEvent);
            onClick();
          },
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

function MapPanTracker({ onManualPan, onPan }: { onManualPan: () => void; onPan: (center: [number, number]) => void }) {
  useMapEvents({
    dragstart() {
      onManualPan();
    },
    dragend: (event) => {
      const center = event.target.getCenter();
      onPan([center.lat, center.lng]);
    },
    zoomend: (event) => {
      const center = event.target.getCenter();
      onPan([center.lat, center.lng]);
    },
  });
  return null;
}

function MissionMapPanTracker({ onPan }: { onPan: (center: [number, number]) => void }) {
  useMapEvents({
    dragend: (event) => {
      const center = event.target.getCenter();
      onPan([center.lat, center.lng]);
    },
    zoomend: (event) => {
      const center = event.target.getCenter();
      onPan([center.lat, center.lng]);
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
  yp,
  onClick,
  onDragStart,
  onDragEnd,
  onMove,
}: {
  waypoint: WaypointMarker;
  vehicle?: Vehicle;
  yp?: Vehicle;
  onClick: () => void;
  onDragStart: () => void;
  onDragEnd: () => void;
  onMove: (lat: number, lon: number) => void;
}) {
  const color = vehicle ? vehicleMarkerColor(vehicle) : "#0f172a";
  
  // Safely fallback to the static waypoint coordinate if the YP or its position is missing
  const lat = waypoint.trackingYP ? (yp?.position?.latitude ?? waypoint.latitude) : waypoint.latitude;
  const lon = waypoint.trackingYP ? (yp?.position?.longitude ?? waypoint.longitude) : waypoint.longitude;
  
  const position = useMemo<[number, number]>(() => [lat, lon], [lat, lon]);
  const icon = useMemo(() => waypointIcon(color), [color]);
  
  return (
    <Marker
      position={position}
      icon={icon}
      zIndexOffset={6000}
      draggable
      eventHandlers={{
        click: (event) => {
          L.DomEvent.stopPropagation(event.originalEvent);
          onClick();
        },
        dragstart: onDragStart,
        dragend: (event) => {
          const newPos = event.target.getLatLng();
          onMove(newPos.lat, newPos.lng);
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
  missionWaypoints: Array<{ latitude: number; longitude: number; altitude: number }>;
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
    missionWaypoints: [],
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
    } else if (vehicle.mode === "mission_plan") {
      if (vehicle.missionWaypoints.length > 0) {
        const nextWaypoint = vehicle.missionWaypoints.shift();
        if (nextWaypoint) {
          vehicle.target = nextWaypoint;
        }
      } else {
        vehicle.mode = "hold";
      }
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
  const yp = vehicles.find((candidate) => candidate.vehicle_id === command.ship_vehicle_id)
    ?? vehicles.find((candidate) => candidate.vehicle_type === "yp");
  const ypPosition = yp ? { latitude: yp.lat, longitude: yp.lon, altitude: yp.alt } : null;
  if (command.type === "rtb") {
    vehicle.mode = "rtb";
    vehicle.manualWaypoint = false;
    vehicle.missionWaypoints = [];
    vehicle.target = yp ? sternTargetForYp(yp, vehicle) : { latitude: 38.984764, longitude: -76.478643, altitude: vehicle.vehicle_type === "uuv" ? -4 : vehicle.vehicle_type === "uav" ? 45 : 0 };
  }
  if (command.type === "waypoint" && command.target) {
    vehicle.mode = "waypoint";
    vehicle.manualWaypoint = true;
    vehicle.missionWaypoints = [];
    vehicle.target = command.target;
  }
  if (command.type === "mission_plan" && command.waypoints && command.waypoints.length > 0) {
    const missionWaypoints = command.waypoints.map((waypoint) => ({
      latitude: waypoint.latitude,
      longitude: waypoint.longitude,
      altitude: waypoint.altitude,
    }));
    const [firstWaypoint, ...remainingWaypoints] = missionWaypoints;
    if (firstWaypoint) {
      vehicle.mode = "mission_plan";
      vehicle.manualWaypoint = true;
      vehicle.target = firstWaypoint;
      vehicle.missionWaypoints = remainingWaypoints;
    }
  }
  if (command.type === "ship_relative_trajectory" && yp && ypPosition && command.local_waypoints?.length) {
    const firstWaypoint = command.local_waypoints[0];
    vehicle.mode = "waypoint";
    vehicle.manualWaypoint = true;
    vehicle.missionWaypoints = [];
    vehicle.target = localToGlobalWaypoint(
      ypPosition.latitude,
      ypPosition.longitude,
      yp.heading,
      ypPosition.altitude,
      firstWaypoint.x,
      firstWaypoint.y,
      firstWaypoint.z,
    );
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

function calculateRelativePosition(ship: Vehicle, target: Vehicle) {
  if (!ship.position || !target.position || ship.heading == null) return null;

  const distance = haversineMeters(
    ship.position.latitude, ship.position.longitude,
    target.position.latitude, target.position.longitude
  );
  
  const trueBearing = bearingDegrees(
    ship.position.latitude, ship.position.longitude,
    target.position.latitude, target.position.longitude
  );

  // Relative bearing: 0 is straight ahead, 90 is starboard, -90 is port
  let relBearingDeg = trueBearing - ship.heading;
  
  // Normalize angle to be between -180 and +180 degrees
  relBearingDeg = ((relBearingDeg + 540) % 360) - 180;
  const relBearingRad = relBearingDeg * (Math.PI / 180);

  // Calculate Forward (Y) and Starboard (X) distances
  const x = distance * Math.cos(relBearingRad);
  const y = -distance * Math.sin(relBearingRad);
  const z = (target.position?.altitude ?? 0) - (ship.position?.altitude ?? 0);

  return { x, y, z, distance };
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
  const hasVideo = Boolean(
    vehicle.video?.enabled && 
    ((Array.isArray(vehicle.video?.streams) && vehicle.video.streams.length > 0) ||
      Boolean(vehicle.video?.playback_url))
  );
  
  const baseSizes: Record<string, [number, number]> = {
    yp:  [120, 60],
    usv: [70, 35],
    ugv: [70, 35],
    uuv: [60, 30],
    uav: [50, 50],
  };

  const baseSize = baseSizes[type] ?? [60, 30];

  const scale = Math.pow(2, Math.min(zoom, 17) - 17);
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
}: {
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
}) {
  const position = vehicle.position;
  const [showColorPalette, setShowColorPalette] = useState(false);
  const [showModeSelector, setShowModeSelector] = useState(false);
  const [draftColor, setDraftColor] = useState(vehicleMarkerColor(vehicle));
  // Checks if the video property exists, is enabled, and has at least one displayable stream
  // (either an explicit streams array or the canonical server-published playback_url)
  const canStreamVideo = Boolean(
    vehicle.video?.enabled && 
    ((Array.isArray(vehicle.video?.streams) && vehicle.video.streams.length > 0) ||
      Boolean(vehicle.video?.playback_url))
  );
  const modalRef = useRef<HTMLDivElement | null>(null);

  // Calculate relative position
  const relativePos = shipVehicle && vehicle.vehicle_id !== shipVehicle.vehicle_id
    ? calculateRelativePosition(shipVehicle, vehicle)
    : null;

  // 1. Setup floating window state (Default to bottom-left)
  const [frame, setFrame] = useState(() => ({
    x: 20,
    y: Math.max(20, window.innerHeight - 480),
    width: Math.min(340, Math.max(280, window.innerWidth - 24)),
  }));

  const clampFrameToViewport = (candidate: typeof frame) => {
    const padding = 12;
    const maxWidth = Math.max(280, Math.min(600, window.innerWidth - padding * 2));
    const width = Math.min(maxWidth, Math.max(280, candidate.width));
    const measuredHeight = modalRef.current?.offsetHeight ?? 420;
    const maxX = Math.max(padding, window.innerWidth - width - padding);
    const maxY = Math.max(padding, window.innerHeight - measuredHeight - padding);

    return {
      width,
      x: Math.min(Math.max(padding, candidate.x), maxX),
      y: Math.min(Math.max(padding, candidate.y), maxY),
    };
  };

  useEffect(() => {
    setFrame((current) => clampFrameToViewport(current));
  }, [vehicle.vehicle_id, showColorPalette, showModeSelector]);

  useEffect(() => {
    const onResize = () => setFrame((current) => clampFrameToViewport(current));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const dragRef = useRef<{
    mode: "move" | "resize";
    pointerId: number;
    startX: number;
    startY: number;
    frame: typeof frame;
  } | null>(null);

  // 2. Drag and Resize Handlers
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
    if (!drag || drag.pointerId !== event.pointerId) return;

    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;

    if (drag.mode === "move") {
      setFrame(clampFrameToViewport({
        ...drag.frame,
        x: drag.frame.x + dx,
        y: drag.frame.y + dy,
      }));
    } else if (drag.mode === "resize") {
      setFrame(clampFrameToViewport({
        ...drag.frame,
        width: drag.frame.width + dx,
      }));
    }
  };

  const endDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) {
      dragRef.current = null;
    }
  };

  return (
    // Note: The <div className="modal-backdrop"> has been completely removed
    <div 
      ref={modalRef}
      className="vehicle-modal" 
      style={{
        position: 'fixed',
        left: frame.x,
        top: frame.y,
        width: frame.width,
        margin: 0,
        zIndex: 5000,
        boxShadow: '0 10px 25px -5px rgba(0, 0, 0, 0.5)',
        cursor: 'default',
        maxHeight: 'calc(100vh - 24px)',
        overflowY: 'auto',
        overscrollBehavior: 'contain'
      }}
      // Stop clicks from falling through to the map underneath
      onMouseDown={(event) => event.stopPropagation()}
      onPointerDown={(event) => event.stopPropagation()}
      onClick={(event) => event.stopPropagation()}
    >
      <div 
        className="modal-header" 
        style={{ cursor: 'grab' }}
        onPointerDown={(e) => startDrag("move", e)}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
      >
        <div className={`type-chip ${vehicle.vehicle_type}`}>{vehicle.vehicle_type.toUpperCase()}</div>
        <div style={{ flex: 1 }}>
          <h2>{vehicle.vehicle_id}</h2>
          <p>{vehicle.connected ? "Connected" : "Last seen offline"}</p>
        </div>
        {/* Stop pointer events on the X button so it doesn't trigger a drag */}
        <button 
          className="icon-button" 
          title="Close" 
          onPointerDown={(e) => e.stopPropagation()} 
          onClick={onClose}
        >
          <X size={20} />
        </button>
      </div>
      
      <div className="metrics">
        <Metric label="Latitude" value={position?.latitude.toFixed(6) ?? "--"} />
        <Metric label="Longitude" value={position?.longitude.toFixed(6) ?? "--"} />
        <Metric label="Altitude" value={`${(position?.altitude ?? 0).toFixed(1)} m`} />
        <Metric label="Heading" value={`${(vehicle.heading ?? 0).toFixed(0)} deg`} />
        <Metric label="Battery" value={vehicle.battery?.percentage == null ? "--" : `${Math.round(vehicle.battery.percentage * 100)}%`} />
        <Metric label="SAR Mission" value={sarMissionActive ? "Running" : "Idle"} />
      </div>

      {relativePos && (
        <div className="relative-metrics" style={{ marginTop: '15px', paddingTop: '15px', borderTop: '1px solid #334155' }}>
          <div style={{ fontSize: '12px', fontWeight: 'bold', color: '#94a3b8', marginBottom: '8px', textTransform: 'uppercase' }}>
            Ship Reference Frame (FLU)
          </div>
          <div className="metrics">
            <Metric label="X (Forward)" value={`${relativePos.x > 0 ? '+' : ''}${relativePos.x.toFixed(1)} m`} />
            <Metric label="Y (Left/Port)" value={`${relativePos.y > 0 ? '+' : ''}${relativePos.y.toFixed(1)} m`} />
            <Metric label="Z (Up)" value={`${relativePos.z > 0 ? '+' : ''}${relativePos.z.toFixed(1)} m`} />
            <Metric label="Radial Dist." value={`${relativePos.distance.toFixed(1)} m`} />
          </div>
        </div>
      )}

      <div className="modal-actions" style={{ marginTop: '15px' }}>
        {canCommand && (
          <button className="secondary" onClick={onEndSar} disabled={!sarMissionActive} title={sarMissionActive ? "Stop active SAR mission" : "No active SAR mission"}>
            <CircleDashed size={18} />
            End SAR Mission
          </button>
        )}
        {canCommand && (
          <button className="danger" onClick={onRtb}>
            <RotateCcw size={18} />
            RTB
          </button>
        )}
        <button className="secondary" onClick={() => setShowColorPalette((value) => !value)}>
          <Brush size={18} />
          Color
        </button>
        {canCommand && VEHICLE_MODES[vehicle.vehicle_type]?.length > 0 && (
          <button className="secondary" onClick={() => setShowModeSelector((value) => !value)}>
            Settings
            {showModeSelector ? " ✕" : ""}
          </button>
        )}
        {canStreamVideo && (
          <button className="stream" onClick={onStreamVideo}>
            <Video size={18} />
            Stream Video
          </button>
        )}
        {canCommand && (
          <button className="primary" onClick={onWaypoint}>
            <Route size={18} />
            Waypoint
          </button>
        )}
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

      {showModeSelector && VEHICLE_MODES[vehicle.vehicle_type]?.length > 0 && (
        <div className="color-panel">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
            {VEHICLE_MODES[vehicle.vehicle_type].map((mode) => (
              <button
                key={mode}
                className="secondary"
                style={{ fontSize: '13px', padding: '6px 8px' }}
                onClick={() => {
                  onSetMode(mode);
                  setShowModeSelector(false);
                }}
              >
                {mode}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Resize Handle at the bottom right */}
      <div 
         style={{
           position: 'absolute',
           bottom: 0,
           right: 0,
           width: '24px',
           height: '24px',
           cursor: 'nwse-resize',
           display: 'flex',
           alignItems: 'flex-end',
           justifyContent: 'flex-end',
           padding: '4px'
         }}
         onPointerDown={(e) => startDrag("resize", e)}
         onPointerMove={moveDrag}
         onPointerUp={endDrag}
      >
         <Maximize2 size={14} color="#64748b" style={{ transform: 'rotate(90deg)' }} />
      </div>
    </div>
  );
}

function UsvVideoViewer({
  vehicleId,
  streams,
  onClose,
}: {
  vehicleId: string;
  streams: { label: string; url: string }[];
  onClose: () => void;
}) {
  const [frame, setFrame] = useState(() => ({
    x: Math.max(16, window.innerWidth - 456),
    y: 120,
    width: Math.min(420, window.innerWidth - 32),
    height: 320,
  }));
  
  // Track which camera URL is currently selected. Defaults to the first camera in the array.
  const [activeUrl, setActiveUrl] = useState<string>(streams[0]?.url || "");

  const dragRef = useRef<{
    mode: "move" | "resize";
    pointerId: number;
    startX: number;
    startY: number;
    frame: typeof frame;
  } | null>(null);
  
  const videoRef = useRef<HTMLVideoElement | null>(null);

  // The WebRTC logic watches 'activeUrl' so it automatically re-negotiates when the user switches cameras
  useEffect(() => {
    const node = videoRef.current;
    if (!node || !activeUrl) return;

    let pc: RTCPeerConnection | null = new RTCPeerConnection();
    let isActive = true;

    const startWebRTC = async () => {
      try {
        pc!.addTransceiver('video', { direction: 'recvonly' });

        pc!.ontrack = (event) => {
          if (node.srcObject !== event.streams[0]) {
            node.srcObject = event.streams[0];
          }
        };

        const offer = await pc!.createOffer();
        await pc!.setLocalDescription(offer);

        const response = await fetch(activeUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/sdp' },
          body: offer.sdp,
        });

        if (!response.ok) throw new Error(`WHEP stream failed: ${response.status}`);
        
        const answerSdp = await response.text();

        if (isActive) {
          await pc!.setRemoteDescription({ type: 'answer', sdp: answerSdp });
        }
      } catch (error) {
        console.error(`WebRTC Error on ${activeUrl}:`, error);
      }
    };

    startWebRTC();

    return () => {
      isActive = false;
      if (pc) {
        pc.close();
        pc = null;
      }
      if (node) node.srcObject = null;
    };
  }, [activeUrl]);

  const updateFrame = (next: typeof frame) => {
    const maxWidth = Math.max(280, window.innerWidth - 24);
    const maxHeight = Math.max(220, window.innerHeight - 24);
    const width = Math.min(maxWidth, Math.max(280, next.width));
    const height = Math.min(maxHeight, Math.max(220, next.height));
    setFrame({
      width, height,
      x: Math.min(Math.max(12, next.x), Math.max(12, window.innerWidth - width - 12)),
      y: Math.min(Math.max(12, next.y), Math.max(12, window.innerHeight - height - 12)),
    });
  };

  const startDrag = (mode: "move" | "resize", event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { mode, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, frame };
  };

  const moveDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (drag.mode === "move") {
      updateFrame({ ...drag.frame, x: drag.frame.x + dx, y: drag.frame.y + dy });
      return;
    }
    updateFrame({ ...drag.frame, width: drag.frame.width + dx, height: drag.frame.height + dy });
  };

  const endDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
  };

  return (
    <section
      className="video-viewer"
      style={{ left: frame.x, top: frame.y, width: frame.width, height: frame.height }}
      onMouseDown={(event) => event.stopPropagation()}
    >
      <header className="video-viewer-header" onPointerDown={(event) => startDrag("move", event)} onPointerMove={moveDrag} onPointerUp={endDrag}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <Video size={16} />
          <strong>{vehicleId}</strong>
          
          {/* Render the dropdown if the vehicle provided more than 1 camera feed */}
          {streams.length > 1 && (
            <select 
              value={activeUrl} 
              onChange={(e) => setActiveUrl(e.target.value)}
              onPointerDown={(e) => e.stopPropagation()} // Prevents dropdown click from dragging the window
              style={{
                background: '#1e293b', 
                color: 'white', 
                border: '1px solid #475569', 
                borderRadius: '4px', 
                padding: '2px 6px',
                fontSize: '12px',
                outline: 'none',
                cursor: 'pointer'
              }}
            >
              {streams.map((stream, index) => (
                <option key={index} value={stream.url}>{stream.label}</option>
              ))}
            </select>
          )}
        </div>
        <button className="icon-button" title="Close stream" onPointerDown={(event) => event.stopPropagation()} onClick={onClose}>
          <X size={17} />
        </button>
      </header>
      <video ref={videoRef} className="video-viewer-media" autoPlay muted playsInline />
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
