import type { Command } from "./types";

export interface ServerSettings {
  message_retention_seconds: number;
  message_cleanup_interval_seconds: number;
  influx_max_write_hz: number;
  tile_max_cache_age_seconds: number;
  rtb_update_hz?: number;
  rtb_stern_distance_m?: number;
  rtb_altitude_m?: number;
  rtb_yp_safe_distance_m?: number;
  land_on_boat_hover_clearance_m?: number;
  land_on_boat_descent_rate_ms?: number;
  land_on_boat_pad_offset_m?: number;
  land_on_boat_alignment_radius_m?: number;
  land_on_boat_auto_disarm?: boolean;
  land_on_boat_touchdown_dwell_s?: number;
  yp_role_vehicle_id?: string | null;
  trail_seconds?: number;
  show_yp_range_rings?: boolean;
  mob_track_seconds?: number;
  mob_swath_m?: number;
  mob_altitude_m?: number;
  mob_corridor_half_width_m?: number;
  mob_takeoff_altitude_m?: number;
  mob_climb_speed_ms?: number;
  rtk_source_type?: "serial" | "tcp" | "udp" | "disabled";
  rtk_host_or_port?: string;
  rtk_network_port?: number;
  rtk_baudrate?: number;
  coordinated_fallback_range_m?: number;
}

export interface RtcmStatus {
  state: "disabled" | "connecting" | "connected" | "stale" | "error";
  source_type: string;
  target: string | null;
  last_frame_at: number | null;
  frame_count: number;
  bytes_total: number;
  error: string | null;
}

// ===== Authentication helpers =====

function apiFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  return fetch(input, { credentials: "include", ...init });
}

async function settingsRequest<T>(path: string, error: string, settings?: object): Promise<T> {
  const response = await apiFetch(path, {
    headers: getAuthHeaders(),
    ...(settings === undefined ? {} : { method: "PUT", body: JSON.stringify(settings) }),
  });
  if (!response.ok) throw new Error(`${error}: ${response.status}`);
  return response.json();
}

export function logout(): void {
  void apiFetch("/api/auth/logout", { method: "POST" });
}

export function getAuthHeaders(): HeadersInit {
  return { "Content-Type": "application/json" };
}

export function websocketUrl(path: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${path}`;
}

export interface AxisCamera {
  id: string;
  label: string;
  online: boolean;
  last_checked: number | null;
  stream_url: string;
  ptz_capable: boolean;
  mount_heading_deg: number;
  pan_deg: number | null;
  tilt_deg: number | null;
  ship_relative_deg: number | null;
  spatial: CameraSpatial;
}

export interface CameraSpatial {
  x_m: number;
  y_m: number;
  z_m: number;
  heading_deg: number;
  tilt_deg: number;
  pan_zero_deg: number;
  hfov_deg: number;
  vfov_deg: number;
}

export async function listAxisCameras(): Promise<AxisCamera[]> {
  const response = await apiFetch("/api/cameras", { headers: getAuthHeaders() });
  if (!response.ok) return [];
  const data = await response.json() as { cameras?: AxisCamera[] };
  return data.cameras ?? [];
}

export async function updateCameraSpatial(spatial: Record<string, CameraSpatial>): Promise<void> {
  const response = await apiFetch("/api/cameras/spatial", {
    method: "PUT", headers: getAuthHeaders(), body: JSON.stringify(spatial),
  });
  if (!response.ok) throw new Error(`camera spatial settings update failed: ${response.status}`);
}

export interface CameraDetection {
  label: string;
  confidence: number;
  box: [number, number, number, number]; // [x1, y1, x2, y2] in source frame pixels
  track_id?: number;
  fusion_id?: number;
  yp_position?: { x_m: number; y_m: number; z_m: number | null; camera_count: number };
}

export interface CameraDetectionUpdate {
  camera_id: string;
  frame_width: number;
  frame_height: number;
  detections: CameraDetection[];
  timestamp: number;
}

export async function sendAxisPtz(cameraId: string, pan: number, tilt: number, zoom = 0): Promise<void> {
  const response = await apiFetch(`/api/cameras/${encodeURIComponent(cameraId)}/ptz`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify({ pan, tilt, zoom }),
  });
  if (!response.ok) throw new Error(`PTZ command failed: ${response.status}`);
}

export async function getCameraTrack(cameraId: string): Promise<boolean> {
  const response = await apiFetch(`/api/cameras/${encodeURIComponent(cameraId)}/track`, { headers: getAuthHeaders() });
  if (!response.ok) return false;
  const data = await response.json() as { tracking?: boolean };
  return data.tracking ?? false;
}

export async function getCameraTrackStates(cameraIds: string[]): Promise<Record<string, boolean>> {
  const states = await Promise.all(cameraIds.map(async (cameraId) => [cameraId, await getCameraTrack(cameraId)] as const));
  return Object.fromEntries(states);
}

export async function setCameraTrack(cameraId: string, enabled: boolean): Promise<boolean> {
  const response = await apiFetch(`/api/cameras/${encodeURIComponent(cameraId)}/track`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify({ enabled }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error ?? `Track toggle failed: ${response.status}`);
  }
  const data = await response.json() as { tracking?: boolean };
  return data.tracking ?? enabled;
}

export async function getCoordinatedCameraTrack(): Promise<boolean> {
  const response = await apiFetch("/api/cameras/coordinated-track", { headers: getAuthHeaders() });
  if (!response.ok) return false;
  return Boolean((await response.json() as { enabled?: boolean }).enabled);
}

export async function setCoordinatedCameraTrack(enabled: boolean): Promise<boolean> {
  const response = await apiFetch("/api/cameras/coordinated-track", {
    method: "POST", headers: getAuthHeaders(), body: JSON.stringify({ enabled }),
  });
  if (!response.ok) throw new Error(`Coordinated tracking update failed: ${response.status}`);
  return Boolean((await response.json() as { enabled?: boolean }).enabled);
}

export interface TrackSettings {
  track_gain: number;
  track_max_speed: number;
  track_deadzone: number;
}

export async function getTrackSettings(): Promise<TrackSettings> {
  const response = await apiFetch("/api/detector/track-settings", { headers: getAuthHeaders() });
  if (!response.ok) return { track_gain: 160, track_max_speed: 60, track_deadzone: 0.04 };
  return response.json();
}

export async function updateTrackSettings(settings: Partial<TrackSettings>): Promise<TrackSettings> {
  const response = await apiFetch("/api/detector/track-settings", {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify(settings),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error ?? `Track settings update failed: ${response.status}`);
  }
  return response.json();
}

export interface DetectorModels {
  models: string[];
  active_model: string;
  conf_threshold: number;
  infer_interval_seconds: number;
}

export async function listDetectorModels(): Promise<DetectorModels> {
  const response = await apiFetch("/api/detector/models", { headers: getAuthHeaders() });
  if (!response.ok) return { models: [], active_model: "", conf_threshold: 0.4, infer_interval_seconds: 0.5 };
  return response.json();
}

export async function selectDetectorModel(model: string): Promise<DetectorModels> {
  const response = await apiFetch("/api/detector/model", {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify({ model }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error ?? `Model selection failed: ${response.status}`);
  }
  return response.json();
}

export async function uploadDetectorModel(file: File): Promise<DetectorModels> {
  const body = new FormData();
  body.append("file", file);
  // No Content-Type header: the browser sets the multipart boundary itself.
  const response = await apiFetch("/api/detector/models", { method: "POST", body });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error ?? `Model upload failed: ${response.status}`);
  }
  return response.json();
}

export async function updateDetectorSettings(settings: Partial<Pick<DetectorModels, "conf_threshold" | "infer_interval_seconds">>): Promise<DetectorModels> {
  const response = await apiFetch("/api/detector/settings", {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify(settings),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error ?? `Settings update failed: ${response.status}`);
  }
  return response.json();
}

// ===== Authentication API =====

export interface LoginResult {
  ok: boolean;
  token_type?: string;
  user?: {
    username: string;
    permissions: string[];
  };
  error?: string;
}

export async function login(username: string, password: string): Promise<LoginResult> {
  const response = await apiFetch("/api/auth/login", {
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
    const response = await apiFetch("/api/auth/me", {
      headers: getAuthHeaders(),
    });
    if (!response.ok) return null;
    return response.json();
  } catch {
    return null;
  }
}

export type PermissionLevel = "view_only" | "waypoint_command" | "mission_planning" | "man_overboard" | "admin";

export type ManagedUser = CurrentUser;

async function authApiResponse(response: Response): Promise<void> {
  if (response.ok) {
    return;
  }
  const payload = await response.json().catch(() => ({}));
  throw new Error(payload.error ?? `Request failed: ${response.status}`);
}

export async function listUsers(): Promise<ManagedUser[]> {
  const response = await apiFetch("/api/auth/users", { headers: getAuthHeaders() });
  await authApiResponse(response);
  const payload = await response.json();
  return payload.users ?? [];
}

export async function createUser(username: string, password: string, permissionLevel: PermissionLevel): Promise<void> {
  const response = await apiFetch("/api/auth/users", {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify({ username, password, permission_level: permissionLevel }),
  });
  await authApiResponse(response);
}

export async function updateUserPermission(username: string, permissionLevel: PermissionLevel): Promise<void> {
  const response = await apiFetch(`/api/auth/users/${encodeURIComponent(username)}/permissions`, {
    method: "PUT",
    headers: getAuthHeaders(),
    body: JSON.stringify({ permission_level: permissionLevel }),
  });
  await authApiResponse(response);
}

export async function updateUserPermissions(username: string, permissions: string[]): Promise<void> {
  const response = await apiFetch(`/api/auth/users/${encodeURIComponent(username)}/permissions`, {
    method: "PUT",
    headers: getAuthHeaders(),
    body: JSON.stringify({ permissions }),
  });
  await authApiResponse(response);
}

export async function updateUserPassword(username: string, password: string): Promise<void> {
  const response = await apiFetch(`/api/auth/users/${encodeURIComponent(username)}/password`, {
    method: "PUT",
    headers: getAuthHeaders(),
    body: JSON.stringify({ password }),
  });
  await authApiResponse(response);
}

export async function deleteUser(username: string): Promise<void> {
  const response = await apiFetch(`/api/auth/users/${encodeURIComponent(username)}`, {
    method: "DELETE",
    headers: getAuthHeaders(),
  });
  await authApiResponse(response);
}

// ===== Settings and configuration =====

export function fetchSettings(): Promise<ServerSettings> {
  return settingsRequest("/api/settings", "settings fetch failed");
}

export function updateSettings(settings: Partial<ServerSettings>): Promise<ServerSettings> {
  return settingsRequest("/api/settings", "settings update failed", settings);
}

export function fetchRtcmStatus(): Promise<RtcmStatus> {
  return settingsRequest("/api/rtcm/status", "rtcm status fetch failed");
}

export async function exportFlightLog(lastHours: number): Promise<Response> {
  const response = await apiFetch(`/api/logs/export?last_hours=${encodeURIComponent(lastHours)}`, {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    let message = `flight log export failed: ${response.status}`;
    try {
      const body = await response.json() as { error?: string };
      if (body.error) message = body.error;
    } catch {
      // Keep the HTTP status message when the server did not return JSON.
    }
    throw new Error(message);
  }
  return response;
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

export function fetchDeconflictionSettings(): Promise<DeconflictionSettings> {
  return settingsRequest("/api/deconfliction/settings", "deconfliction settings fetch failed");
}

export function updateDeconflictionSettings(settings: Partial<DeconflictionSettings>): Promise<DeconflictionSettings> {
  return settingsRequest("/api/deconfliction/settings", "deconfliction settings update failed", settings);
}

export async function fetchDeconflictionConflicts(): Promise<{ enabled: boolean; conflicts: Array<{ low_priority_vehicle: string; high_priority_vehicle: string }> }> {
  const response = await apiFetch("/api/deconfliction/conflicts", {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    throw new Error(`deconfliction conflicts fetch failed: ${response.status}`);
  }
  return response.json();
}

/** Designate a vehicle as the YP (mother vessel), or pass null to clear. */
export async function setYpRole(vehicleId: string | null): Promise<{ ok: boolean; vehicle_id: string | null }> {
  const response = await apiFetch("/api/yp/role", {
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
export async function triggerMOB(vehicleId?: string, trackSeconds?: number, swathM?: number, altM?: number, corridorHalfWidthM?: number, takeoffAltitudeM?: number, climbSpeedMs?: number): Promise<MobResult> {
  const body: Record<string, unknown> = {};
  if (vehicleId) body.vehicle_id = vehicleId;
  if (trackSeconds !== undefined) body.track_seconds = trackSeconds;
  if (swathM !== undefined) body.swath_m = swathM;
  if (altM !== undefined) body.altitude_m = altM;
  if (corridorHalfWidthM !== undefined) body.corridor_half_width_m = corridorHalfWidthM;
  if (takeoffAltitudeM !== undefined) body.takeoff_altitude_m = takeoffAltitudeM;
  if (climbSpeedMs !== undefined) body.climb_speed_ms = climbSpeedMs;
  const response = await apiFetch("/api/sar/mob", {
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
  const response = await apiFetch("/api/sitl", {
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
  const response = await apiFetch("/api/sitl", {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) return { ok: false, error: data.error ?? `HTTP ${response.status}` };
  return data;
}

export async function disconnectSITL(vehicleId: string): Promise<void> {
  await apiFetch(`/api/sitl/${encodeURIComponent(vehicleId)}`, {
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
  const response = await apiFetch("/api/serial-ports", {
    headers: getAuthHeaders(),
  });
  if (!response.ok) return [];
  const data = await response.json();
  return data.ports ?? [];
}
