export type VehicleType = "uav" | "uavf" | "usv" | "ugv" | "uuv" | "yp";

export interface Position {
  latitude: number;
  longitude: number;
  altitude: number;
  stamp?: number;
}

export interface VehicleMessage {
  type: string;
  stamp: number;
  msg: Record<string, unknown>;
}

export interface RelativeWaypoint {
  x: number;
  y: number;
  z: number;
}

export interface Vehicle {
  vehicle_id: string;
  vehicle_type: VehicleType;
  connected: boolean;
  last_seen: number;
  last_seen_age?: number;
  video?: {
    vehicle_id: string;
    enabled?: boolean;
    streams?: {
      label: string;
      url: string;
    }[];
  };
  position?: Position;
  history?: Position[];
  heading?: number;
  battery?: {
    percentage?: number;
    voltage?: number;
    current?: number;
  };
  messages: Record<string, VehicleMessage>;
}

export interface Command {
  type: "rtb" | "waypoint" | "trajectory" | "search_grid" | "ship_relative_trajectory";
  target?: {
    latitude: number;
    longitude: number;
    altitude: number;
  };
  // search_grid fields
  lat?: number;
  lon?: number;
  grid_size_m?: number;
  swath_m?: number;
  altitude_m?: number;
  ship_vehicle_id?: string;
  local_waypoints?: RelativeWaypoint[];
  arrival_radius_m?: number;
  update_hz?: number;
}
