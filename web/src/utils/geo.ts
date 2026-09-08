import type { Vehicle } from "../types";

const EARTH_RADIUS_M = 6371000;

export function haversineMeters(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const p1 = (lat1 * Math.PI) / 180;
  const p2 = (lat2 * Math.PI) / 180;
  const dp = ((lat2 - lat1) * Math.PI) / 180;
  const dl = ((lon2 - lon1) * Math.PI) / 180;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return EARTH_RADIUS_M * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

export function bearingDegrees(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const p1 = (lat1 * Math.PI) / 180;
  const p2 = (lat2 * Math.PI) / 180;
  const dl = ((lon2 - lon1) * Math.PI) / 180;
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (Math.atan2(y, x) * 180) / Math.PI;
}

export function destinationPoint(lat: number, lon: number, bearing: number, distance: number): { latitude: number; longitude: number } {
  const angular = distance / EARTH_RADIUS_M;
  const theta = (bearing * Math.PI) / 180;
  const p1 = (lat * Math.PI) / 180;
  const l1 = (lon * Math.PI) / 180;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(angular) + Math.cos(p1) * Math.sin(angular) * Math.cos(theta));
  const l2 = l1 + Math.atan2(Math.sin(theta) * Math.sin(angular) * Math.cos(p1), Math.cos(angular) - Math.sin(p1) * Math.sin(p2));
  return { latitude: (p2 * 180) / Math.PI, longitude: (l2 * 180) / Math.PI };
}

export function calculateRelativePosition(ship: Vehicle, target: Vehicle) {
  if (!ship.position || !target.position || ship.heading == null) return null;
  const distance = haversineMeters(ship.position.latitude, ship.position.longitude, target.position.latitude, target.position.longitude);
  const trueBearing = bearingDegrees(ship.position.latitude, ship.position.longitude, target.position.latitude, target.position.longitude);
  const relBearingDeg = ((trueBearing - ship.heading + 540) % 360) - 180;
  const relBearingRad = relBearingDeg * (Math.PI / 180);
  return {
    x: distance * Math.cos(relBearingRad),
    y: -distance * Math.sin(relBearingRad),
    z: (target.position.altitude ?? 0) - (ship.position.altitude ?? 0),
    distance,
  };
}

export function localToGlobalWaypoint(shipLat: number, shipLon: number, shipHeading: number, shipAlt: number, localX: number, localY: number, localZ: number) {
  const distanceMeters = Math.hypot(localX, localY);
  const relativeAngleDeg = (Math.atan2(localX, localY) * 180) / Math.PI;
  const globalCoord = destinationPoint(shipLat, shipLon, (shipHeading + relativeAngleDeg + 360) % 360, distanceMeters);
  return { latitude: globalCoord.latitude, longitude: globalCoord.longitude, altitude: shipAlt + localZ };
}
