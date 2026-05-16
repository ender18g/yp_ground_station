export type VehicleType = "uav" | "usv" | "uuv" | "yp";

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
  type: "rtb" | "waypoint" | "trajectory";
  target?: {
    latitude: number;
    longitude: number;
    altitude: number;
  };
}
