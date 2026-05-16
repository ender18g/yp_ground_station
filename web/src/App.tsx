import L from "leaflet";
import {
  Battery,
  Crosshair,
  LocateFixed,
  RotateCcw,
  Route,
  Settings,
  ShipWheel,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { CircleMarker, LayersControl, MapContainer, Marker, Polyline, Popup, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";

import { sendCommand, websocketUrl } from "./api";
import type { Command, Vehicle, VehicleType } from "./types";

const USNA_CENTER: [number, number] = [38.9822, -76.4819];

export function App() {
  const [vehicles, setVehicles] = useState<Record<string, Vehicle>>({});
  const [connected, setConnected] = useState(false);
  const [selected, setSelected] = useState<Vehicle | null>(null);
  const [pendingWaypointFor, setPendingWaypointFor] = useState<string | null>(null);
  const [trailSeconds, setTrailSeconds] = useState(30);
  const [showSettings, setShowSettings] = useState(false);
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

  const vehicleList = useMemo(() => Object.values(vehicles).filter((vehicle) => vehicle.position), [vehicles]);
  const yp = vehicleList.find((vehicle) => vehicle.vehicle_type === "yp");
  const center: [number, number] = yp?.position ? [yp.position.latitude, yp.position.longitude] : USNA_CENTER;

  const command = (vehicleId: string, body: Command) => sendCommand(wsRef.current, vehicleId, body);

  return (
    <div className={pendingWaypointFor ? "app picking" : "app"}>
      <MapContainer center={center} zoom={15} minZoom={3} maxZoom={19} zoomControl className="map">
        <LayersControl position="bottomleft">
          <LayersControl.BaseLayer checked name="Offline tiles">
            <TileLayer url="/tiles/{z}/{x}/{y}.png" attribution="Offline OpenStreetMap tiles" />
          </LayersControl.BaseLayer>
          <LayersControl.BaseLayer name="Local MBTiles server">
            <TileLayer url="/local-map/styles/basic-preview/{z}/{x}/{y}.png" attribution="Local MBTiles" />
          </LayersControl.BaseLayer>
          <LayersControl.BaseLayer name="Online OpenStreetMap">
            <TileLayer url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" attribution="OpenStreetMap contributors" />
          </LayersControl.BaseLayer>
        </LayersControl>
        <MapCommander
          pendingWaypointFor={pendingWaypointFor}
          selectedVehicle={pendingWaypointFor ? vehicles[pendingWaypointFor] : null}
          onWaypoint={(vehicleId, lat, lon, altitude) => {
            command(vehicleId, { type: "waypoint", target: { latitude: lat, longitude: lon, altitude } });
            setPendingWaypointFor(null);
          }}
        />
        <FitAllControl vehicles={vehicleList} />
        {vehicleList.map((vehicle) => (
          <VehicleLayer
            key={vehicle.vehicle_id}
            vehicle={vehicle}
            trailSeconds={trailSeconds}
            onClick={() => setSelected(vehicle)}
          />
        ))}
      </MapContainer>

      <div className="topbar">
        <div className="brand">
          <ShipWheel size={22} />
          <div>
            <strong>YP Ground Station</strong>
            <span>{vehicleList.length} tracked</span>
          </div>
        </div>
        <div className={connected ? "status online" : "status offline"}>
          {connected ? <Wifi size={16} /> : <WifiOff size={16} />}
          <span>{connected ? "Server linked" : "Reconnecting"}</span>
        </div>
        <button className="icon-button" title="Settings" onClick={() => setShowSettings((value) => !value)}>
          <Settings size={19} />
        </button>
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

      {pendingWaypointFor && (
        <div className="target-banner">
          <Crosshair size={18} />
          <span>Click the map to send {pendingWaypointFor} a waypoint</span>
          <button title="Cancel waypoint" onClick={() => setPendingWaypointFor(null)}>
            <X size={17} />
          </button>
        </div>
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
      {vehicle.vehicle_type === "yp" && <CircleMarker center={[position.latitude, position.longitude]} radius={18} pathOptions={{ color, weight: 2, fillOpacity: 0.05 }} />}
    </>
  );
}

function MapCommander({
  pendingWaypointFor,
  selectedVehicle,
  onWaypoint,
}: {
  pendingWaypointFor: string | null;
  selectedVehicle: Vehicle | null;
  onWaypoint: (vehicleId: string, lat: number, lon: number, altitude: number) => void;
}) {
  useMapEvents({
    click(event) {
      if (!pendingWaypointFor) {
        return;
      }
      const altitude = selectedVehicle?.position?.altitude ?? 0;
      onWaypoint(pendingWaypointFor, event.latlng.lat, event.latlng.lng, altitude);
    },
  });
  return null;
}

function FitAllControl({ vehicles }: { vehicles: Vehicle[] }) {
  const map = useMap();

  useEffect(() => {
    if (vehicles.length === 0) {
      return;
    }
    const bounds = L.latLngBounds(vehicles.map((vehicle) => [vehicle.position!.latitude, vehicle.position!.longitude]));
    map.fitBounds(bounds.pad(0.25), { maxZoom: 16 });
  }, []);

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
        <div class="vehicle-marker ${type}">
          <div class="marker-arrow" style="transform: rotate(${heading}deg)">▲</div>
          <div class="marker-label">${type.toUpperCase()}</div>
        </div>
        <div class="alt-label">${altitude.toFixed(0)} m</div>
      </div>
    `,
  });
}

function vehicleColor(type: VehicleType): string {
  return {
    uav: "#f97316",
    usv: "#16a34a",
    uuv: "#eab308",
    yp: "#2563eb",
  }[type];
}
