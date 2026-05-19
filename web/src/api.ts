import type { Command } from "./types";

export interface ServerSettings {
  message_retention_seconds: number;
  message_cleanup_interval_seconds: number;
  influx_max_write_hz: number;
  tile_max_cache_age_seconds: number;
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

export async function updateSettings(settings: Pick<ServerSettings, "message_retention_seconds">): Promise<ServerSettings> {
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
