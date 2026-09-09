import type { Vehicle, VehicleType } from "../types";

export function vehicleColor(type: VehicleType): string {
  return {
    uav: "#dc2626",
    uavf: "#b91c1c",
    usv: "#16a34a",
    ugv: "#b45309",
    uuv: "#eab308",
    yp: "#6b7280",
  }[type];
}

export function vehicleMarkerColor(vehicle: Vehicle): string {
  return vehicle.marker_color ?? vehicleColor(vehicle.vehicle_type);
}
