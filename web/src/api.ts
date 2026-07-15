import type { Command } from "./types";

export interface ServerSettings {
  message_retention_seconds: number;
  message_cleanup_interval_seconds: number;
  influx_max_write_hz: number;
  tile_max_cache_age_seconds: number;
  rtb_update_hz?: number;
  yp_role_vehicle_id?: string | null;
}

export function websocketUrl(path: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${path}`;
}

export async function fetchSettings(): Promise<ServerSettings> {
  const response = await fetch("/api/settings");
  if (!response.ok) {
    throw new Error(`settings fetch failed: ${response.status}`);
  }
  return response.json();
}

export async function updateSettings(settings: Pick<ServerSettings, "message_retention_seconds"> | Pick<ServerSettings, "rtb_update_hz"> | Pick<ServerSettings, "message_retention_seconds" | "rtb_update_hz">): Promise<ServerSettings> {
  const response = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  if (!response.ok) {
    throw new Error(`settings update failed: ${response.status}`);
  }
  return response.json();
}

/** Designate a vehicle as the YP (mother vessel), or pass null to clear. */
export async function setYpRole(vehicleId: string | null): Promise<{ ok: boolean; vehicle_id: string | null }> {
  const response = await fetch("/api/yp/role", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vehicle_id: vehicleId }),
  });
  if (!response.ok) {
    throw new Error(`YP role update failed: ${response.status}`);
  }
  return response.json();
}

export function sendCommand(ws: WebSocket | null, vehicleId: string, command: Command): void {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    return;
  }
  ws.send(
    JSON.stringify({
      op: "command",
      vehicle_id: vehicleId,
      command,
    }),
  );
}

export interface MobResult {
  ok: boolean;
  vehicle_id?: string;
  error?: string;
}

/** Trigger a Man Overboard search via the server's SAR endpoint. */
export async function triggerMOB(vehicleId?: string, trackSeconds?: number, swathM?: number, altM?: number): Promise<MobResult> {
  const body: Record<string, unknown> = {};
  if (vehicleId) body.vehicle_id = vehicleId;
  if (trackSeconds !== undefined) body.track_seconds = trackSeconds;
  if (swathM !== undefined) body.swath_m = swathM;
  if (altM !== undefined) body.altitude_m = altM;
  const response = await fetch("/api/sar/mob", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
    return { ok: false, error: err.error ?? `HTTP ${response.status}` };
  }
  return response.json();
}

// ---------------------------------------------------------------------------
// SITL bridge API
// ---------------------------------------------------------------------------

export interface SITLBridge {
  vehicle_id: string;
  url: string;
  status: "connecting" | "connected" | "error" | "disconnected";
  frame: string | null;
  autopilot: string | null;
  vehicle_type: string;
  error: string | null;
}

export interface ConnectSITLResult {
  ok: boolean;
  vehicle_id?: string;
  url?: string;
  error?: string;
}

export async function listSITLBridges(): Promise<SITLBridge[]> {
  const response = await fetch("/api/sitl");
  if (!response.ok) return [];
  const data = await response.json();
  return data.bridges ?? [];
}

export async function connectSITL(url: string, vehicleId?: string): Promise<ConnectSITLResult> {
  const body: Record<string, string> = { url };
  if (vehicleId) body.vehicle_id = vehicleId;
  const response = await fetch("/api/sitl", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) return { ok: false, error: data.error ?? `HTTP ${response.status}` };
  return data;
}

export async function disconnectSITL(vehicleId: string): Promise<void> {
  await fetch(`/api/sitl/${encodeURIComponent(vehicleId)}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Serial port listing
// ---------------------------------------------------------------------------

export interface SerialPortInfo {
  device: string;
  description: string;
  hwid: string;
}

export async function listSerialPorts(): Promise<SerialPortInfo[]> {
  const response = await fetch("/api/serial-ports");
  if (!response.ok) return [];
  const data = await response.json();
  return data.ports ?? [];
}
