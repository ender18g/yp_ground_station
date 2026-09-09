import L from "leaflet";
import {
  AlertTriangle,
  Cable,
  Crosshair,
  EthernetPort,
  Grid3X3,
  Layers,
  Loader2,
  MessageSquare,
  Radio,
  Route,
  Save,
  Settings,
  Ship,
  Wifi,
  WifiOff,
  Map as MapIcon,
  LogOut,
  Users,
} from "lucide-react";
import { lazy, Suspense, useEffect, useMemo, useRef, useState, useCallback } from "react";
import { MapContainer, Polyline, TileLayer, useMap, useMapEvents } from "react-leaflet";

import { connectSITL, disconnectSITL, exportFlightLog, fetchSettings, getCurrentUser, listSITLBridges, sendCommand, setYpRole, triggerMOB, updateSettings, logout as logoutUser, fetchDeconflictionSettings, updateDeconflictionSettings } from "./api";
import type { CurrentUser, SITLBridge } from "./api";
import type { Command, Position, Vehicle, VehicleType } from "./types";
import Login from "./Login";
const UserManagement = lazy(() => import("./UserManagement"));
import { destinationPoint } from "./utils/geo";
import { useTelemetrySocket } from "./hooks/useTelemetrySocket";
import { MessageDrawer, type StreamMessage } from "./components/MessageDrawer";
import { SITLPanel } from "./components/SITLPanel";
import { VehicleModal } from "./components/VehicleModal";
import { VideoViewer } from "./components/VideoViewer";
import { FitAllControl, FollowYpCenter, SarPatternOverlay, VehicleLayer, WaypointCrosshair, YpRangeRings, type WaypointMarker } from "./components/map/VehicleLayers";
import { vehicleMarkerColor } from "./utils/vehicleStyle";
import { WeatherRadarLayer, WindLayer } from "./components/map/OverlayLayers";
import { createDemoVehicles, demoVehicleSnapshot, handleDemoCommand, stepDemoVehicle, updateDemoVehicleColor, type DemoMessagePayload, type DemoVehicle } from "./services/demo";

const MissionPlannerMode = lazy(() => import("./components/MissionPlannerMode").then((module) => ({ default: module.MissionPlannerMode })));
const WaypointPlanner = lazy(() => import("./components/WaypointPlanner").then((module) => ({ default: module.WaypointPlanner })));


const USNA_CENTER: [number, number] = [38.9822, -76.4819];
const MAX_MESSAGE_LOG = 700;
const DEMO_MODE = import.meta.env.VITE_STATIC_DEMO === "true" || window.location.pathname.startsWith("/demo") || window.location.search.includes("demo=true");
/** View-only mode: live data but commands blocked for real (non-sim) vehicles. */
const VIEW_MODE = !DEMO_MODE && (window.location.pathname.startsWith("/view") || window.location.search.includes("view=true"));
/** Returns true if a vehicle ID belongs to a docker-spawned sim vehicle. */
function isSimVehicle(vehicleId: string): boolean {
  return vehicleId.startsWith("sim-");
}
const BRAND_LOGO_URL = `${import.meta.env.BASE_URL}logos/usna_crest_jhublue.png`;
type MapBase = "satellite" | "street";
type MapSource = "auto" | "cache" | "online";

interface MapActionMenuState {
  lat: number;
  lon: number;
  x: number;
  y: number;
}

const DEMO_USER: CurrentUser = {
  username: "demo",
  active: true,
  permissions: [],
  created_at: null,
  last_login: null,
};

export function App() {
  const [currentUser, setCurrentUser] = useState<CurrentUser | null | undefined>(DEMO_MODE ? DEMO_USER : undefined);
  const [sessionVersion, setSessionVersion] = useState(0);

  useViewportLayoutSync();

  useEffect(() => {
    if (DEMO_MODE) return;
    let cancelled = false;
    void getCurrentUser().then((user) => {
      if (!cancelled) setCurrentUser(user);
    });
    return () => { cancelled = true; };
  }, [sessionVersion]);

  const onLogout = useCallback(() => {
    logoutUser();
    setCurrentUser(null);
  }, []);

  if (currentUser === undefined) {
    return <div className="loading-state">Checking session...</div>;
  }
  if (!currentUser) {
    return <Login onLogin={() => { setCurrentUser(undefined); setSessionVersion((value) => value + 1); }} />;
  }

  return <GroundStation currentUser={currentUser} onLogout={onLogout} />;
}

function GroundStation({ currentUser, onLogout }: { currentUser: CurrentUser; onLogout: () => void }) {
  const isPhoneViewer = useIsPhoneViewer();
  const [vehicles, setVehicles] = useState<Record<string, Vehicle>>({});
  const [selected, setSelected] = useState<Vehicle | null>(null);
  const [trailSeconds, setTrailSeconds] = useState(45);
  const [showSettings, setShowSettings] = useState(false);
  const [showFlightLogOptions, setShowFlightLogOptions] = useState(false);
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
  const [flightLogHours, setFlightLogHours] = useState(8);
  const [flightLogExporting, setFlightLogExporting] = useState(false);
  const [flightLogError, setFlightLogError] = useState<string | null>(null);
  const [rtbUpdateHz, setRtbUpdateHz] = useState(2.0);
  const [rtbSternDistanceM, setRtbSternDistanceM] = useState(35);
  const [rtbAltitudeM, setRtbAltitudeM] = useState(30);
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
  const [settingsTab, setSettingsTab] = useState<"display" | "mob" | "vessel" | "deconfliction" | "rtk">("display");
  const [rtkSourceType, setRtkSourceType] = useState<"serial" | "tcp" | "udp" | "disabled">("serial");
  const [rtkHostOrPort, setRtkHostOrPort] = useState("/dev/ttyACM0");
  const [rtkNetworkPort, setRtkNetworkPort] = useState(9000);
  const [rtkBaudrate, setRtkBaudrate] = useState(115200);
  const [deconflictionEnabled, setDeconflictionEnabled] = useState(false);
  const [deconflictionSettingsLoaded, setDeconflictionSettingsLoaded] = useState(DEMO_MODE);
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
  const { connected: socketConnected, socketRef: wsRef } = useTelemetrySocket({
    enabled: !DEMO_MODE,
    onAuthenticationExpired: onLogout,
    onPayload: (payload) => {
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
          const newHistory: Position[] = msgType.includes("NavSatFix") && pos
            ? [...prevHistory, { latitude: pos.latitude, longitude: pos.longitude, altitude: pos.altitude, stamp }].slice(-500)
            : prevHistory;
          return { ...current, [incoming.vehicle_id]: { ...incoming, history: newHistory } };
        });
        if (payload.message) setMessageLog((current) => [streamMessageFromPayload(payload.message as Parameters<typeof streamMessageFromPayload>[0]), ...current].slice(0, MAX_MESSAGE_LOG));
      }
      if (payload.op === "command_ack") {
        setMessageLog((current) => [streamMessageFromCommandAck(payload), ...current].slice(0, MAX_MESSAGE_LOG));
        const ackVehicleId = payload.vehicle_id as string | undefined;
        const ackCommandType = (payload.command as { type?: string } | undefined)?.type;
        if (ackVehicleId && ackCommandType) updateSarMissionState(ackVehicleId, ackCommandType);
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
        setVehicles((current) => { const next = { ...current }; delete next[removedId]; return next; });
        setSarMissionActiveByVehicle((current) => { const next = { ...current }; delete next[removedId]; return next; });
        setSitlBridges((current) => { const next = { ...current }; delete next[removedId]; return next; });
      }
      if (payload.op === "sar_pattern") {
        setSarPatterns((current) => ({ ...current, [payload.vehicle_id as string]: { patternType: payload.pattern_type as string, waypoints: payload.waypoints as [number, number][] } }));
      }
      if (payload.op === "waypoint_overlay") {
        const waypoint = payload.waypoint as WaypointMarker;
        setWaypointMarkers((current) => ({ ...current, [waypoint.vehicle_id]: waypoint }));
      }
      if (payload.op === "mission_plan_overlay") {
        setMissionPlans((current) => ({ ...current, [payload.vehicle_id as string]: payload.waypoints as [number, number][] }));
      }
      if (payload.op === "mission_plan_cleared") {
        setMissionPlans((current) => { const next = { ...current }; delete next[payload.vehicle_id as string]; return next; });
      }
      if (payload.op === "sar_pattern_cleared") {
        setSarPatterns((current) => { const next = { ...current }; delete next[payload.vehicle_id as string]; return next; });
      }
      if (payload.op === "vehicle_disconnected") {
        setVehicles((current) => ({ ...current, [payload.vehicle_id as string]: { ...current[payload.vehicle_id as string], connected: false } }));
      }
      if (payload.op === "video_stream_update") {
        const incoming = payload.video as Vehicle["video"] & { vehicle_id?: string };
        const vehicleId = incoming?.vehicle_id;
        if (!vehicleId) return;
        setVehicles((current) => {
          const currentVehicle = current[vehicleId];
          if (!currentVehicle) return current;
          return { ...current, [vehicleId]: { ...currentVehicle, video: incoming } };
        });
      }
      if (payload.op === "video_stream_removed") {
        const vehicleId = payload.vehicle_id as string;
        setVehicles((current) => {
          const currentVehicle = current[vehicleId];
          if (!currentVehicle || !currentVehicle.video) return current;
          const nextVehicle = { ...currentVehicle };
          delete nextVehicle.video;
          return { ...current, [vehicleId]: nextVehicle };
        });
      }
    },
  });
  const connected = DEMO_MODE || socketConnected;
  const demoSimsRef = useRef<DemoVehicle[]>([]);
  const localVehicleColorsRef = useRef<Record<string, string>>({});
  const settingsPanelRef = useRef<HTMLDivElement | null>(null);
  const flightLogPanelRef = useRef<HTMLDivElement | null>(null);
  const flightLogButtonRef = useRef<HTMLButtonElement | null>(null);
  const sitlPanelRef = useRef<HTMLDivElement | null>(null);
  const messageDrawerRef = useRef<HTMLDivElement | null>(null);
  const mapMenuRef = useRef<HTMLDivElement | null>(null);
  const settingsButtonRef = useRef<HTMLButtonElement | null>(null);
  const sitlButtonRef = useRef<HTMLButtonElement | null>(null);
  const messagesButtonRef = useRef<HTMLButtonElement | null>(null);
  const mapMenuToggleRef = useRef<HTMLButtonElement | null>(null);
  
  const [activeTab, setActiveTab] = useState<"map" | "mission" | "planner">("map");

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
        if (typeof serverSettings.rtb_altitude_m === "number") {
          setRtbAltitudeM(serverSettings.rtb_altitude_m);
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
        if (typeof serverSettings.rtk_source_type === "string") {
          setRtkSourceType(serverSettings.rtk_source_type as any);
        }
        if (typeof serverSettings.rtk_host_or_port === "string") {
          setRtkHostOrPort(serverSettings.rtk_host_or_port);
        }
        if (typeof serverSettings.rtk_network_port === "number") {
          setRtkNetworkPort(serverSettings.rtk_network_port);
        }
        if (typeof serverSettings.rtk_baudrate === "number") {
          setRtkBaudrate(serverSettings.rtk_baudrate);
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
        rtb_altitude_m: rtbAltitudeM,
        mob_track_seconds: mobTrackSeconds,
        mob_swath_m: mobSwathM,
        mob_altitude_m: mobAltM,
        mob_corridor_half_width_m: mobCorridorHalfWidthM,
        mob_takeoff_altitude_m: mobTakeoffAltitudeM,
        mob_climb_speed_ms: mobClimbSpeedMs,
        yp_role_vehicle_id: ypRoleVehicleId,
        rtk_source_type: rtkSourceType,
        rtk_host_or_port: rtkHostOrPort,
        rtk_network_port: rtkNetworkPort,
        rtk_baudrate: rtkBaudrate,
      }).catch(() => undefined);
    }, 350);
    return () => window.clearTimeout(timeout);
  }, [trailSeconds, showYpRangeRings, messageRetentionMinutes, rtbUpdateHz, rtbSternDistanceM, rtbAltitudeM, mobTrackSeconds, mobSwathM, mobAltM, mobCorridorHalfWidthM, mobTakeoffAltitudeM, mobClimbSpeedMs, ypRoleVehicleId, rtkSourceType, rtkHostOrPort, rtkNetworkPort, rtkBaudrate, settingsLoaded]);

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
        setDeconflictionSettingsLoaded(true);
      })
      .catch(() => setDeconflictionSettingsLoaded(true));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (DEMO_MODE || !deconflictionSettingsLoaded) return;
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
  }, [deconflictionEnabled, deconflictionGlobalRadius, deconflictionRadii, deconflictionOrbitRadius, deconflictionMaxPause, deconflictionSettingsLoaded]);

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

      if (showFlightLogOptions) {
        const insideFlightLogPanel = flightLogPanelRef.current?.contains(target) ?? false;
        const onFlightLogButton = flightLogButtonRef.current?.contains(target) ?? false;
        if (!insideFlightLogPanel && !onFlightLogButton) {
          setShowFlightLogOptions(false);
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
  }, [showSettings, showFlightLogOptions, showSITL, showMessages, mapMenuExpanded]);

  const vehicleList = useMemo(() => Object.values(vehicles).filter((vehicle) => vehicle.position), [vehicles]);
  const yp = vehicleList.find((vehicle) => vehicle.vehicle_type === "yp");
  const ypGpsLinked = Boolean(yp?.connected);
  const filteredMessages = useMemo(() => filterMessages(messageLog, topicFilters), [messageLog, topicFilters]);
  const renderedMapSource = DEMO_MODE ? "online" : mapSource;
  const mapLayer = useMemo(() => tileLayerFor(mapBase, renderedMapSource), [mapBase, renderedMapSource]);

  const command = (vehicleId: string, body: Command): boolean => {
    if (VIEW_MODE && !isSimVehicle(vehicleId)) {
      return false;
    }
    updateSarMissionState(vehicleId, body.type);
    if (DEMO_MODE) {
      handleDemoCommand(demoSimsRef.current, vehicleId, body);
      return true;
    }
    sendCommand(wsRef.current, vehicleId, body);
    return true;
  };

  const sendWaypoint = (vehicleId: string, lat: number, lon: number, altitude?: number) => {
    // Right-click "Send Vehicle" should interrupt any active SAR mission first.
    command(vehicleId, { type: "cancel_sar" });
    if (!command(vehicleId, { type: "waypoint", target: { latitude: lat, longitude: lon, altitude: altitude ?? vehicles[vehicleId]?.position?.altitude ?? 0 } })) return;
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
    if (DEMO_MODE) {
      setMobError("MOB dispatch requires the live ground station and vehicle connections.");
      return;
    }
    if (VIEW_MODE && !isSimVehicle(mobVehicleId)) {
      setMobError("View-only mode can dispatch only a simulated vehicle.");
      return;
    }
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
      },
    }));
    setSelected((current) => (current?.vehicle_id === vehicleId ? ({ ...current, marker_color: color } ) : current));
  };

  const saveFlightLog = async () => {
    setFlightLogExporting(true);
    setFlightLogError(null);
    try {
      const response = await exportFlightLog(flightLogHours);
      const blob = await response.blob();
      const filename = response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1]
        ?? `yp-flight-log-last-${flightLogHours}h.jsonl.gz`;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setShowFlightLogOptions(false);
    } catch (error) {
      setFlightLogError(error instanceof Error ? error.message : "Flight log export failed");
    } finally {
      setFlightLogExporting(false);
    }
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
          <FollowYpCenter yp={yp} enabled={followYp} onCenterChange={setMapCenter} />
          <FitAllControl vehicles={vehicleList} />
          {showYpRangeRings && <YpRangeRings yp={yp} />}
          {(Object.entries(sarPatterns) as Array<[string, { patternType: string; waypoints: [number, number][] }]>).map(([vehicleId, pattern]) => (
            <SarPatternOverlay
              key={vehicleId}
              vehicleId={vehicleId}
              patternType={pattern.patternType}
              waypoints={pattern.waypoints}
              color={(vehicles[vehicleId] as Vehicle | undefined)?.marker_color ?? "#f97316"}
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
        <Suspense fallback={<div className="loading-state">Loading mission planner...</div>}>
          <MissionPlannerMode
          center={mapCenter}
          zoom={mapZoom}
          onZoomChange={setMapZoom}
          onCenterChange={setMapCenter}
          mapLayer={mapLayer}
          vehicles={vehicleList}
          missionPlans={missionPlans}
          yp={yp}
          showWeatherRadar={showWeatherRadar}
          showWindOverlay={showWindOverlay}
          onToggleWind={() => setShowWindOverlay((value) => !value)}
          showYpRangeRings={showYpRangeRings}
          sarPatterns={sarPatterns}
          waypointMarkers={waypointMarkers}
          trailSeconds={trailSeconds}
          canCommandVehicle={(vehicleId) => !VIEW_MODE || isSimVehicle(vehicleId)}
          onCommand={command}
          />
        </Suspense>
      ) : (
        <Suspense fallback={<div className="loading-state">Loading waypoint planner...</div>}>
          <WaypointPlanner 
           yp={yp} 
           vehicles={vehicleList.filter(v => v.vehicle_type !== "yp")} 
           onCommand={command} 
          />
        </Suspense>
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

      <div className="trident-tagline">Telemetry, Remote Intelligence, Data, Electronic Navigation, and Tasking - Yard Patrol</div>

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
          {currentUser?.permissions.includes("manage_settings") && !VIEW_MODE && (
            <div className="flight-log-control">
              <button
                ref={flightLogButtonRef}
                className={showFlightLogOptions ? "icon-button active" : "icon-button"}
                title="Save Flight Log"
                aria-label="Save Flight Log"
                onClick={() => { setShowFlightLogOptions((value) => !value); setShowSettings(false); setShowSITL(false); }}
              >
                <Save size={19} />
              </button>
              {showFlightLogOptions && (
                <div className="flight-log-panel" ref={flightLogPanelRef}>
                  <div className="panel-title">
                    <Save size={17} />
                    <strong>Save Flight Log</strong>
                  </div>
                  <label>
                    Include previous
                    <span>{flightLogHours} {flightLogHours === 1 ? "hour" : "hours"}</span>
                  </label>
                  <input
                    aria-label="Flight log duration"
                    min={1}
                    max={24}
                    step={1}
                    type="range"
                    value={flightLogHours}
                    disabled={flightLogExporting}
                    onChange={(event) => setFlightLogHours(Number(event.target.value))}
                  />
                  <button className="flight-log-save-action" disabled={flightLogExporting} onClick={() => void saveFlightLog()}>
                    {flightLogExporting ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
                    {flightLogExporting ? "Saving..." : "Save log file"}
                  </button>
                </div>
              )}
            </div>
          )}
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
          {!VIEW_MODE && !DEMO_MODE && (
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
            onClick={() => { setShowSettings((value) => !value); setShowFlightLogOptions(false); setShowSITL(false); }}
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
          {!DEMO_MODE && <button className="icon-button" title="Logout" onClick={onLogout}>
            <LogOut size={19} />
          </button>}
        </div>
        {flightLogError && <div className="flight-log-error" role="alert">{flightLogError}</div>}
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
        <Suspense fallback={null}>
          <UserManagement onClose={() => setShowUserManagement(false)} />
        </Suspense>
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
            <button
              className={settingsTab === "rtk" ? "settings-tab active" : "settings-tab"}
              onClick={() => setSettingsTab("rtk")}
            >
              RTK Correction
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
          {settingsTab === "rtk" && (
            <>
              <label>
                Source Mode
                <select
                  value={rtkSourceType}
                  disabled={DEMO_MODE}
                  onChange={(e) => setRtkSourceType(e.target.value as any)}
                >
                  <option value="disabled">Disabled</option>
                  <option value="serial">USB / Serial Port</option>
                  <option value="tcp">TCP Base Station / Caster</option>
                  <option value="udp">UDP Listener</option>
                </select>
              </label>
              {rtkSourceType !== "disabled" && (
                <>
                  <label>
                    {rtkSourceType === "serial" ? "Serial Port Device" : "Host IP / Interface Address"}
                    <input
                      type="text"
                      value={rtkHostOrPort}
                      disabled={DEMO_MODE}
                      placeholder={rtkSourceType === "serial" ? "/dev/ttyACM0" : "192.168.1.100"}
                      onChange={(e) => setRtkHostOrPort(e.target.value)}
                    />
                  </label>
                  {rtkSourceType === "serial" && (
                    <label>
                      Baud Rate
                      <select
                        value={rtkBaudrate}
                        disabled={DEMO_MODE}
                        onChange={(e) => setRtkBaudrate(Number(e.target.value))}
                      >
                        <option value={9600}>9600</option>
                        <option value={19200}>19200</option>
                        <option value={38400}>38400</option>
                        <option value={57600}>57600</option>
                        <option value={115200}>115200</option>
                        <option value={230400}>230400</option>
                        <option value={460800}>460800</option>
                        <option value={921600}>921600</option>
                      </select>
                    </label>
                  )}
                  {(rtkSourceType === "tcp" || rtkSourceType === "udp") && (
                    <label>
                      Network Port
                      <input
                        type="number"
                        min={1}
                        max={65535}
                        value={rtkNetworkPort}
                        disabled={DEMO_MODE}
                        onChange={(e) => setRtkNetworkPort(Number(e.target.value))}
                      />
                    </label>
                  )}
                </>
              )}
              <p className="settings-hint">
                Streams raw RTCM3 correction frames to all connected MAVLink vehicles for RTK precision navigation.
              </p>
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
                disabled={DEMO_MODE}
                onChange={(e) => {
                  const newId = e.target.value || null;
                  setYpRoleVehicleId(newId);
                  setYpRole(newId).catch(() => undefined);
                }}
              >
                <option value="">- dedicated yp_gps service -</option>
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
              <label>
                RTB altitude
                <span>{rtbAltitudeM} m</span>
              </label>
              <input min={5} max={150} step={5} type="range" value={rtbAltitudeM} disabled={DEMO_MODE} onChange={(event) => setRtbAltitudeM(Number(event.target.value))} />
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

      {streamVehicleId && (
        <VideoViewer
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

      {/* Fixed red MOB button, available from the map view. */}
      {activeTab === "map" && <button
        className="mob-button"
        disabled={VIEW_MODE && !Object.values(vehicles).some((vehicle) => vehicle.vehicle_type !== "yp" && vehicle.vehicle_type !== "ugv" && vehicle.connected !== false && isSimVehicle(vehicle.vehicle_id))}
        title="Man Overboard - dispatch SAR search"
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
      </button>}

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
                    <option value="">- nearest available -</option>
                    {commandable.map((v) => (
                      <option key={v.vehicle_id} value={v.vehicle_id}>{v.vehicle_id}</option>
                    ))}
                  </select>
                ) : (
                  <div className="mob-no-vehicles">No connected vehicles - server will choose automatically</div>
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
                {mobSending ? "Dispatching..." : "MAN OVERBOARD!"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function streamMessageFromPayload(payload: DemoMessagePayload | {
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

function useViewportLayoutSync(): void {
  useEffect(() => {
    const updateViewport = () => {
      const width = Math.round(window.visualViewport?.width ?? window.innerWidth);
      document.documentElement.dataset.viewportWidth = String(width);
      document.documentElement.style.setProperty("--viewport-width", `${width}px`);
    };

    updateViewport();
    window.addEventListener("resize", updateViewport);
    window.visualViewport?.addEventListener("resize", updateViewport);
    return () => {
      window.removeEventListener("resize", updateViewport);
      window.visualViewport?.removeEventListener("resize", updateViewport);
      delete document.documentElement.dataset.viewportWidth;
      document.documentElement.style.removeProperty("--viewport-width");
    };
  }, []);
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
  // Maps slider 0-100 to 5-500 m on a log scale for finer control near 5 m
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

function MapZoomTracker({ onZoom }: { onZoom: (zoom: number) => void }) {
  useMapEvents({
    zoomend: (event) => onZoom(event.target.getZoom()),
  });
  return null;
}

function waypointOffset(lat: number, lon: number, index: number, total: number): { latitude: number; longitude: number } {
  if (total <= 1) {
    return { latitude: lat, longitude: lon };
  }
  const radius = 4 + Math.floor(index / 6) * 3;
  const bearing = (360 / total) * index;
  return destinationPoint(lat, lon, bearing, radius);
}

function withLocalVehicleColor(vehicle: Vehicle, localColors: Record<string, string>): Vehicle {
  const color = localColors[vehicle.vehicle_id];
  return color ? ({ ...vehicle, marker_color: color } ) : vehicle;
}
