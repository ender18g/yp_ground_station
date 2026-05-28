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

export interface Vehicle {
  vehicle_id: string;
  vehicle_type: VehicleType;
  connected: boolean;
  last_seen: number;
  last_seen_age?: number;
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
  type: "rtb" | "waypoint" | "trajectory" | "search_grid";
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
}
