import type { Command } from "./types";

export interface ServerSettings {
  message_retention_seconds: number;
  message_cleanup_interval_seconds: number;
  influx_max_write_hz: number;
  tile_max_cache_age_seconds: number;
  rtb_update_hz?: number;
  yp_role_vehicle_id?: string | null;
}

// ===== Authentication helpers =====

export function getAuthToken(): string | null {
  return localStorage.getItem("auth_token");
}

export function getUsername(): string | null {
  return localStorage.getItem("username");
}

export function isAuthenticated(): boolean {
  return !!getAuthToken();
}

export function logout(): void {
  localStorage.removeItem("auth_token");
  localStorage.removeItem("username");
}

export function getAuthHeaders(): HeadersInit {
  const token = getAuthToken();
  return token
    ? {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`,
      }
    : { "Content-Type": "application/json" };
}

export function websocketUrl(path: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const token = getAuthToken();
  const tokenParam = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${protocol}//${window.location.host}${path}${tokenParam}`;
}

// ===== Authentication API =====

export interface LoginResult {
  ok: boolean;
  access_token?: string;
  token_type?: string;
  user?: {
    username: string;
    permissions: string[];
  };
  error?: string;
}

export async function login(username: string, password: string): Promise<LoginResult> {
  const response = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  return response.json();
}

export interface CurrentUser {
  username: string;
  active: boolean;
  permissions: string[];
  created_at: string | null;
  last_login: string | null;
}

export async function getCurrentUser(): Promise<CurrentUser | null> {
  try {
    const response = await fetch("/api/auth/me", {
      headers: getAuthHeaders(),
    });
    if (!response.ok) return null;
    return response.json();
  } catch {
    return null;
  }
}

export type PermissionLevel = "view_only" | "waypoint_command" | "mission_planning" | "man_overboard" | "admin";

export interface ManagedUser {
  username: string;
  active: boolean;
  permissions: string[];
  created_at: string | null;
  last_login: string | null;
}

async function authApiResponse(response: Response): Promise<void> {
  if (response.ok) {
    return;
  }
  const payload = await response.json().catch(() => ({}));
  throw new Error(payload.error ?? `Request failed: ${response.status}`);
}

export async function listUsers(): Promise<ManagedUser[]> {
  const response = await fetch("/api/auth/users", { headers: getAuthHeaders() });
  await authApiResponse(response);
  const payload = await response.json();
  return payload.users ?? [];
}

export async function createUser(username: string, password: string, permissionLevel: PermissionLevel): Promise<void> {
  const response = await fetch("/api/auth/users", {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify({ username, password, permission_level: permissionLevel }),
  });
  await authApiResponse(response);
}

export async function updateUserPermission(username: string, permissionLevel: PermissionLevel): Promise<void> {
  const response = await fetch(`/api/auth/users/${encodeURIComponent(username)}/permissions`, {
    method: "PUT",
    headers: getAuthHeaders(),
    body: JSON.stringify({ permission_level: permissionLevel }),
  });
  await authApiResponse(response);
}

export async function updateUserPermissions(username: string, permissions: string[]): Promise<void> {
  const response = await fetch(`/api/auth/users/${encodeURIComponent(username)}/permissions`, {
    method: "PUT",
    headers: getAuthHeaders(),
    body: JSON.stringify({ permissions }),
  });
  await authApiResponse(response);
}

export async function updateUserPassword(username: string, password: string): Promise<void> {
  const response = await fetch(`/api/auth/users/${encodeURIComponent(username)}/password`, {
    method: "PUT",
    headers: getAuthHeaders(),
    body: JSON.stringify({ password }),
  });
  await authApiResponse(response);
}

export async function deleteUser(username: string): Promise<void> {
  const response = await fetch(`/api/auth/users/${encodeURIComponent(username)}`, {
    method: "DELETE",
    headers: getAuthHeaders(),
  });
  await authApiResponse(response);
}

// ===== Settings and configuration =====

export async function fetchSettings(): Promise<ServerSettings> {
  const response = await fetch("/api/settings", {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    throw new Error(`settings fetch failed: ${response.status}`);
  }
  return response.json();
}

export async function updateSettings(settings: Pick<ServerSettings, "message_retention_seconds"> | Pick<ServerSettings, "rtb_update_hz"> | Pick<ServerSettings, "message_retention_seconds" | "rtb_update_hz">): Promise<ServerSettings> {
  const response = await fetch("/api/settings", {
    method: "PUT",
    headers: getAuthHeaders(),
    body: JSON.stringify(settings),
  });
  if (!response.ok) {
    throw new Error(`settings update failed: ${response.status}`);
  }
  return response.json();
}

// ===== Deconfliction API =====

export interface DeconflictionSettings {
  id?: number;
  enabled: boolean;
  global_radius_m: number;
  radius_per_type: Record<string, number>;
  orbit_radius_m: number;
  max_pause_duration_s: number;
}

export async function fetchDeconflictionSettings(): Promise<DeconflictionSettings> {
  const response = await fetch("/api/deconfliction/settings", {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    throw new Error(`deconfliction settings fetch failed: ${response.status}`);
  }
  return response.json();
}

export async function updateDeconflictionSettings(settings: Partial<DeconflictionSettings>): Promise<DeconflictionSettings> {
  const response = await fetch("/api/deconfliction/settings", {
    method: "PUT",
    headers: getAuthHeaders(),
    body: JSON.stringify(settings),
  });
  if (!response.ok) {
    throw new Error(`deconfliction settings update failed: ${response.status}`);
  }
  return response.json();
}

export async function fetchDeconflictionConflicts(): Promise<{ enabled: boolean; conflicts: Array<{ low_priority_vehicle: string; high_priority_vehicle: string }> }> {
  const response = await fetch("/api/deconfliction/conflicts", {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    throw new Error(`deconfliction conflicts fetch failed: ${response.status}`);
  }
  return response.json();
}

/** Designate a vehicle as the YP (mother vessel), or pass null to clear. */
export async function setYpRole(vehicleId: string | null): Promise<{ ok: boolean; vehicle_id: string | null }> {
  const response = await fetch("/api/yp/role", {
    method: "POST",
    headers: getAuthHeaders(),
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
    headers: getAuthHeaders(),
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
  camera_host?: string | null;
}

export interface ConnectSITLResult {
  ok: boolean;
  vehicle_id?: string;
  url?: string;
  camera_host?: string | null;
  error?: string;
}

export async function listSITLBridges(): Promise<SITLBridge[]> {
  const response = await fetch("/api/sitl", {
    headers: getAuthHeaders(),
  });
  if (!response.ok) return [];
  const data = await response.json();
  return data.bridges ?? [];
}

export async function connectSITL(url: string, vehicleId?: string, cameraHost?: string): Promise<ConnectSITLResult> {
  const body: Record<string, string> = { url };
  if (vehicleId) body.vehicle_id = vehicleId;
  if (cameraHost) body.camera_host = cameraHost;
  const response = await fetch("/api/sitl", {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) return { ok: false, error: data.error ?? `HTTP ${response.status}` };
  return data;
}

export async function disconnectSITL(vehicleId: string): Promise<void> {
  await fetch(`/api/sitl/${encodeURIComponent(vehicleId)}`, {
    method: "DELETE",
    headers: getAuthHeaders(),
  });
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
  const response = await fetch("/api/serial-ports", {
    headers: getAuthHeaders(),
  });
  if (!response.ok) return [];
  const data = await response.json();
  return data.ports ?? [];
}
