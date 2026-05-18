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

export interface LifeguardAgent {
  id: string;
  frame_type: string;
  connected: boolean;
  active_mission: string | null;
}

export interface LifeguardMissionConfig {
  default_waypoint_altitude: number;
  default_swath_width: number;
}

export interface LifeguardShipConfig {
  track_history_minutes: number;
  mob_corridor_half_width_m: number;
  mob_takeoff_altitude_m: number;
  mob_climb_speed_ms: number;
}

export interface LifeguardConfig {
  agents: Array<{ name: string; connection_string: string; frame_type: string }>;
  mission: LifeguardMissionConfig;
  mavlink: { baudrate: number; source_system_id: number };
  ship: LifeguardShipConfig;
}

export interface LifeguardStatus {
  op: "lifeguard_status";
  agent_id: string | null;
  message: string;
  level: "info" | "warn" | "error";
  stamp: number;
}

export interface LifeguardPath {
  op: "lifeguard_path";
  agent_id: string;
  path: Array<[number, number]>;
}
