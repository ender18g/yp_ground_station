import { bearingDegrees, destinationPoint, haversineMeters, localToGlobalWaypoint } from "../utils/geo";
import type { Command, Vehicle, VehicleType } from "../types";

export interface DemoMessagePayload {
  vehicle_id: string;
  vehicle_type: VehicleType;
  topic: string;
  type: string;
  stamp: number;
  msg: Record<string, unknown>;
}

export interface DemoVehicle {
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

const YP_DEMO_SPEED_MPS = 5 * 0.514444;
const YP_DEMO_HEADING = 330;
const DEMO_KEEP_IN_RANGE_M = 200;

export function createDemoVehicles(): DemoVehicle[] {
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

function createDemoVehicle(vehicle_id: string, vehicle_type: VehicleType, lat: number, lon: number, alt: number, heading: number, speed: number, batteryDrainPerSecond: number, battery = 0.86): DemoVehicle {
  return { vehicle_id, vehicle_type, lat, lon, alt, heading, speed, battery: vehicle_type === "yp" ? 1 : battery, batteryDrainPerSecond, marker_color: vehicleColor(vehicle_type), manualWaypoint: false, target: randomDemoTarget(lat, lon, alt), missionWaypoints: [], mode: "loiter", history: [], messages: {}, localX: 0, localY: 0 };
}

export function stepDemoVehicle(vehicle: DemoVehicle, dt: number, stamp: number, vehicles: DemoVehicle[]): DemoMessagePayload[] {
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
  if (vehicle.mode === "rtb" && yp) vehicle.target = sternTargetForYp(yp, vehicle);
  else if (!vehicle.manualWaypoint && yp) {
    const rangeFromYp = haversineMeters(vehicle.lat, vehicle.lon, yp.lat, yp.lon);
    const targetRange = haversineMeters(vehicle.target.latitude, vehicle.target.longitude, yp.lat, yp.lon);
    if (rangeFromYp > DEMO_KEEP_IN_RANGE_M || targetRange > DEMO_KEEP_IN_RANGE_M) vehicle.target = randomDemoTargetNearYp(yp, vehicle);
  }

  const distance = haversineMeters(vehicle.lat, vehicle.lon, vehicle.target.latitude, vehicle.target.longitude);
  if (distance < Math.max(3, vehicle.speed * dt * 2)) {
    if (vehicle.mode === "rtb") vehicle.target = yp ? sternTargetForYp(yp, vehicle) : vehicle.target;
    else if (vehicle.mode === "mission_plan") {
      const nextWaypoint = vehicle.missionWaypoints.shift();
      if (nextWaypoint) vehicle.target = nextWaypoint;
      else vehicle.mode = "hold";
    } else if (vehicle.manualWaypoint) vehicle.mode = "hold";
    else vehicle.target = yp ? randomDemoTargetNearYp(yp, vehicle) : randomDemoTarget(vehicle.lat, vehicle.lon, vehicle.alt);
  } else {
    const bearing = bearingDegrees(vehicle.lat, vehicle.lon, vehicle.target.latitude, vehicle.target.longitude);
    vehicle.heading = smoothDegrees(vehicle.heading, bearing, Math.min(1, dt * 1.6));
    const travel = Math.min(distance, vehicle.speed * dt);
    const next = destinationPoint(vehicle.lat, vehicle.lon, vehicle.heading, travel);
    vehicle.lat = next.latitude;
    vehicle.lon = next.longitude;
    vehicle.alt += Math.max(-1, Math.min(1, vehicle.target.altitude - vehicle.alt)) * Math.min(1, dt);
    if (vehicle.vehicle_type === "usv") vehicle.alt = 0;
    if (vehicle.vehicle_type === "uuv") vehicle.alt = Math.min(-1, vehicle.alt);
    vehicle.localX += Math.sin((vehicle.heading * Math.PI) / 180) * travel;
    vehicle.localY += Math.cos((vehicle.heading * Math.PI) / 180) * travel;
  }
  vehicle.battery = Math.max(0.05, vehicle.battery - dt * vehicle.batteryDrainPerSecond);
  vehicle.history = [...(vehicle.history ?? []), { stamp, latitude: vehicle.lat, longitude: vehicle.lon, altitude: vehicle.alt }].slice(-500);
  return recordDemoMessages(vehicle, stamp);
}

export function demoVehicleSnapshot(vehicle: DemoVehicle): Vehicle {
  return { vehicle_id: vehicle.vehicle_id, vehicle_type: vehicle.vehicle_type, connected: true, last_seen: Date.now() / 1000, last_seen_age: 0, position: { latitude: vehicle.lat, longitude: vehicle.lon, altitude: vehicle.alt }, history: vehicle.history, heading: vehicle.heading, battery: { percentage: vehicle.battery, voltage: 22.2 * vehicle.battery, current: -4 }, messages: vehicle.messages, marker_color: vehicle.marker_color } as Vehicle & { marker_color: string };
}

export function handleDemoCommand(vehicles: DemoVehicle[], vehicleId: string, command: Command): void {
  const vehicle = vehicles.find((candidate) => candidate.vehicle_id === vehicleId);
  if (!vehicle) return;
  const yp = vehicles.find((candidate) => candidate.vehicle_id === command.ship_vehicle_id) ?? vehicles.find((candidate) => candidate.vehicle_type === "yp");
  const ypPosition = yp ? { latitude: yp.lat, longitude: yp.lon, altitude: yp.alt } : null;
  if (command.type === "rtb") { vehicle.mode = "rtb"; vehicle.manualWaypoint = false; vehicle.missionWaypoints = []; vehicle.target = yp ? sternTargetForYp(yp, vehicle) : { latitude: 38.984764, longitude: -76.478643, altitude: vehicle.vehicle_type === "uuv" ? -4 : vehicle.vehicle_type === "uav" ? 45 : 0 }; }
  if (command.type === "waypoint" && command.target) { vehicle.mode = "waypoint"; vehicle.manualWaypoint = true; vehicle.missionWaypoints = []; vehicle.target = command.target; }
  if (command.type === "mission_plan" && command.waypoints?.length) {
    const missionWaypoints = command.waypoints.map((waypoint) => ({ latitude: waypoint.latitude, longitude: waypoint.longitude, altitude: waypoint.altitude }));
    const [firstWaypoint, ...remainingWaypoints] = missionWaypoints;
    if (firstWaypoint) { vehicle.mode = "mission_plan"; vehicle.manualWaypoint = true; vehicle.target = firstWaypoint; vehicle.missionWaypoints = remainingWaypoints; }
  }
  if (command.type === "ship_relative_trajectory" && yp && ypPosition && command.local_waypoints?.length) {
    const firstWaypoint = command.local_waypoints[0];
    vehicle.mode = "waypoint"; vehicle.manualWaypoint = true; vehicle.missionWaypoints = [];
    vehicle.target = localToGlobalWaypoint(ypPosition.latitude, ypPosition.longitude, yp.heading, ypPosition.altitude, firstWaypoint.x, firstWaypoint.y, firstWaypoint.z);
  }
}

export function updateDemoVehicleColor(vehicles: DemoVehicle[], vehicleId: string, color: string): void {
  const vehicle = vehicles.find((candidate) => candidate.vehicle_id === vehicleId);
  if (vehicle) vehicle.marker_color = color;
}

function recordDemoMessages(vehicle: DemoVehicle, stamp: number): DemoMessagePayload[] {
  const messages = demoMessages(vehicle, stamp);
  for (const message of messages) vehicle.messages[message.topic] = { type: message.type, stamp, msg: message.msg };
  return messages;
}

function demoMessages(vehicle: DemoVehicle, stamp: number): DemoMessagePayload[] {
  const topic = (suffix: string) => `/vehicles/${vehicle.vehicle_id}/${suffix}`;
  const quat = yawToQuaternion(vehicle.heading);
  return [
    wrapDemoMessage(vehicle, topic("heartbeat"), "yp_ground_station/msg/Heartbeat", stamp, { mode: vehicle.mode, armed: true }),
    wrapDemoMessage(vehicle, topic("navsatfix"), "sensor_msgs/msg/NavSatFix", stamp, { latitude: vehicle.lat, longitude: vehicle.lon, altitude: vehicle.alt, heading: vehicle.heading, status: { status: 0, service: 1 } }),
    wrapDemoMessage(vehicle, topic("pose"), "geometry_msgs/msg/Pose", stamp, { position: { x: vehicle.localX, y: vehicle.localY, z: vehicle.alt }, orientation: quat, heading: vehicle.heading }),
    wrapDemoMessage(vehicle, topic("battery"), "sensor_msgs/msg/BatteryState", stamp, { voltage: 22.2 * vehicle.battery, current: -4, percentage: vehicle.battery, present: true }),
    wrapDemoMessage(vehicle, topic("trajectory"), "trajectory_msgs/msg/MultiDOFJointTrajectory", stamp, { points: [{ transforms: [{ translation: { x: vehicle.localX, y: vehicle.localY, z: vehicle.alt }, rotation: quat }] }] }),
  ];
}

function wrapDemoMessage(vehicle: DemoVehicle, topic: string, type: string, stamp: number, msg: Record<string, unknown>): DemoMessagePayload {
  return { vehicle_id: vehicle.vehicle_id, vehicle_type: vehicle.vehicle_type, topic, type, stamp, msg };
}

function seedForwardDemoWaypoints(vehicles: DemoVehicle[]): void {
  const yp = vehicles.find((vehicle) => vehicle.vehicle_type === "yp");
  if (!yp) return;
  const forwardOffsets = [{ distance: 120, lateral: -65 }, { distance: 175, lateral: 55 }];
  const aftOffsets = [{ distance: 80, lateral: -45 }, { distance: 115, lateral: 45 }, { distance: 150, lateral: 0 }];
  let forwardIndex = 0; let aftIndex = 0;
  vehicles.filter((vehicle) => vehicle.vehicle_type !== "yp").forEach((vehicle) => {
    const useForwardTarget = vehicle.vehicle_type === "uav";
    const offset = useForwardTarget ? forwardOffsets[forwardIndex % forwardOffsets.length] : aftOffsets[aftIndex % aftOffsets.length];
    if (useForwardTarget) forwardIndex += 1; else aftIndex += 1;
    const axisPoint = destinationPoint(yp.lat, yp.lon, useForwardTarget ? yp.heading : yp.heading + 180, offset.distance);
    const target = destinationPoint(axisPoint.latitude, axisPoint.longitude, yp.heading + 90, offset.lateral);
    vehicle.target = { latitude: target.latitude, longitude: target.longitude, altitude: vehicle.vehicle_type === "uuv" ? -7 : vehicle.vehicle_type === "uav" ? vehicle.alt : 0 };
    vehicle.mode = "waypoint"; vehicle.manualWaypoint = true;
  });
}

function randomDemoTarget(lat: number, lon: number, alt: number): DemoVehicle["target"] { return { latitude: lat + (Math.random() - 0.5) * 0.002, longitude: lon + (Math.random() - 0.5) * 0.002, altitude: alt }; }
function randomDemoTargetNearYp(yp: DemoVehicle, vehicle: DemoVehicle): DemoVehicle["target"] {
  const target = destinationPoint(yp.lat, yp.lon, Math.random() * 360, 50 + Math.random() * 130);
  return { latitude: target.latitude, longitude: target.longitude, altitude: vehicle.vehicle_type === "uuv" ? -6 - Math.random() * 8 : vehicle.vehicle_type === "uav" ? 35 + Math.random() * 25 : 0 };
}
function sternTargetForYp(yp: DemoVehicle, vehicle: DemoVehicle): DemoVehicle["target"] {
  const stern = destinationPoint(yp.lat, yp.lon, yp.heading + 180, 35);
  const lateralOffset = vehicle.vehicle_type === "uav" ? 12 : vehicle.vehicle_type === "uuv" ? -12 : 0;
  const target = lateralOffset === 0 ? stern : destinationPoint(stern.latitude, stern.longitude, yp.heading + 90, lateralOffset);
  return { latitude: target.latitude, longitude: target.longitude, altitude: vehicle.vehicle_type === "uuv" ? -5 : vehicle.vehicle_type === "uav" ? 35 : 0 };
}
function smoothDegrees(current: number, target: number, ratio: number): number { const delta = ((((target - current) % 360) + 540) % 360) - 180; return (current + delta * ratio + 360) % 360; }
function yawToQuaternion(yawDeg: number): Record<string, number> { const half = (yawDeg * Math.PI) / 360; return { x: 0, y: 0, z: Math.sin(half), w: Math.cos(half) }; }

function vehicleColor(vehicleType: VehicleType): string { return { uav: "#dc2626", uavf: "#b91c1c", usv: "#16a34a", ugv: "#b45309", uuv: "#eab308", yp: "#6b7280" }[vehicleType]; }
function assignedVehicleColor(vehicleType: VehicleType, index: number): string { return lightenHex(vehicleColor(vehicleType), Math.min(index * 0.18, 0.5)); }
function lightenHex(hex: string, amount: number): string { const clean = hex.replace("#", ""); const red = parseInt(clean.slice(0, 2), 16); const green = parseInt(clean.slice(2, 4), 16); const blue = parseInt(clean.slice(4, 6), 16); const mix = (value: number) => Math.round(value + (255 - value) * amount); return `#${[mix(red), mix(green), mix(blue)].map((value) => value.toString(16).padStart(2, "0")).join("")}`; }
