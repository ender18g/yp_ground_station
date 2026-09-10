import L from "leaflet";
import { Battery, LocateFixed } from "lucide-react";
import { useEffect, useMemo } from "react";
import {
  Circle,
  CircleMarker,
  Marker,
  Polyline,
  Popup,
  Tooltip,
  useMap,
} from "react-leaflet";
import type { Vehicle, VehicleType } from "../../types";
import { vehicleMarkerColor } from "../../utils/vehicleStyle";

const LOW_BATTERY_THRESHOLD = 0.25;

export interface WaypointMarker {
  vehicle_id: string;
  latitude: number;
  longitude: number;
  trackingYP?: boolean;
}

export function VehicleLayer({
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
      {trail.length > 1 && (
        <Polyline
          positions={trail}
          pathOptions={{ color, weight: 3, opacity: 0.75 }}
        />
      )}
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
        <CircleMarker
          center={[position.latitude, position.longitude]}
          radius={18}
          pathOptions={{ color, weight: 2, fillOpacity: 0.05 }}
          interactive={false}
        />
      )}
    </>
  );
}

export function FollowYpCenter({
  yp,
  enabled,
  onCenterChange,
}: {
  yp?: Vehicle;
  enabled: boolean;
  onCenterChange?: (center: [number, number]) => void;
}) {
  const map = useMap();
  const latitude = yp?.position?.latitude;
  const longitude = yp?.position?.longitude;

  useEffect(() => {
    if (!enabled || latitude == null || longitude == null) {
      return;
    }
    map.setView([latitude, longitude], map.getZoom(), { animate: false });
    onCenterChange?.([latitude, longitude]);
  }, [enabled, latitude, longitude, map, onCenterChange]);

  return null;
}

export function FitAllControl({ vehicles }: { vehicles: Vehicle[] }) {
  const map = useMap();

  return (
    <button
      className="fit-control"
      title="Fit all vehicles"
      onClick={() => {
        if (vehicles.length === 0) {
          return;
        }
        const bounds = L.latLngBounds(
          vehicles.map((vehicle) => [
            vehicle.position!.latitude,
            vehicle.position!.longitude,
          ]),
        );
        map.fitBounds(bounds.pad(0.25), { maxZoom: 17 });
      }}
    >
      <LocateFixed size={18} />
    </button>
  );
}

export function YpRangeRings({ yp }: { yp?: Vehicle }) {
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

export function SarPatternOverlay({
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
        <Tooltip
          permanent
          direction="top"
          offset={[0, -8]}
          className="sar-label-tooltip"
        >
          {label} - {vehicleId}
        </Tooltip>
        <Popup className="sar-clear-popup">
          <div className="sar-clear-popup-inner">
            <span>{label}</span>
            <span className="sar-clear-popup-vehicle">{vehicleId}</span>
            <button className="sar-clear-btn" onClick={onClear}>
              Clear pattern
            </button>
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

export function WaypointCrosshair({
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
  const lat = waypoint.trackingYP
    ? (yp?.position?.latitude ?? waypoint.latitude)
    : waypoint.latitude;
  const lon = waypoint.trackingYP
    ? (yp?.position?.longitude ?? waypoint.longitude)
    : waypoint.longitude;

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

function vehicleIcon(vehicle: Vehicle, isPhoneViewer: boolean, zoom: number) {
  const type = vehicle.vehicle_type;
  const heading = vehicle.heading ?? 0;
  const altitude = vehicle.position?.altitude ?? 0;
  const color = vehicleMarkerColor(vehicle);
  const lowBattery =
    vehicle.vehicle_type !== "yp" &&
    (vehicle.battery?.percentage ?? 1) <= LOW_BATTERY_THRESHOLD;
  const hasVideo = Boolean(
    vehicle.video?.enabled &&
    ((Array.isArray(vehicle.video?.streams) &&
      vehicle.video.streams.length > 0) ||
      Boolean(vehicle.video?.playback_url)),
  );

  const baseSizes: Record<string, [number, number]> = {
    yp: [120, 60],
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
