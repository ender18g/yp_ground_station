export interface MissionWaypoint {
  id: string;
  latitude: number;
  longitude: number;
  altitude: number;
  itemType: "waypoint" | "takeoff" | "loiter_time" | "land" | "rtl" | "do_jump";
  commandIdOverride: number | null;
  param3: number;
  jumpTargetIndex: number;
  jumpRepeatCount: number;
  holdTimeS: number;
  acceptanceRadiusM: number;
  yawDeg: number | null;
}

export type MissionItemType = MissionWaypoint["itemType"];

export function commandIdFor(itemType: MissionWaypoint["itemType"]): number {
  return {
    waypoint: 16,
    loiter_time: 19,
    rtl: 20,
    land: 21,
    takeoff: 22,
    do_jump: 177,
  }[itemType];
}

export function itemTypeFor(commandId: number): MissionWaypoint["itemType"] {
  if (commandId === 22) return "takeoff";
  if (commandId === 19) return "loiter_time";
  if (commandId === 21) return "land";
  if (commandId === 20) return "rtl";
  if (commandId === 177) return "do_jump";
  return "waypoint";
}

function missionParameters(waypoint: MissionWaypoint): number[] {
  const commandId =
    waypoint.commandIdOverride ?? commandIdFor(waypoint.itemType);
  const isDoJump = commandId === 177;
  return [
    isDoJump ? waypoint.jumpTargetIndex : waypoint.holdTimeS,
    isDoJump ? waypoint.jumpRepeatCount : waypoint.acceptanceRadiusM,
    waypoint.param3,
    waypoint.yawDeg ?? 0,
    waypoint.latitude,
    waypoint.longitude,
    waypoint.altitude,
  ];
}

export function toQgcPlan(
  waypoints: MissionWaypoint[],
  defaultAltitude: number,
): Record<string, unknown> {
  return {
    fileType: "Plan",
    geoFence: { polygons: [], circles: [], version: 2 },
    rallyPoints: { points: [], version: 2 },
    version: 1,
    mission: {
      cruiseSpeed: 10,
      firmwareType: 12,
      hoverSpeed: 5,
      plannedHomePosition: [0, 0, 0],
      vehicleType: 2,
      version: 2,
      items: waypoints.map((waypoint, index) => {
        const commandId =
          waypoint.commandIdOverride ?? commandIdFor(waypoint.itemType);
        return {
          AMSLAltAboveTerrain: null,
          Altitude: waypoint.altitude,
          AltitudeMode: 1,
          autoContinue: true,
          command: commandId,
          doJumpId: index + 1,
          frame: 3,
          params: missionParameters(waypoint),
          type: "SimpleItem",
        };
      }),
      defaultAltitude,
    },
  };
}

export function toWpl(waypoints: MissionWaypoint[]): string {
  const lines = ["QGC WPL 110"];
  lines.push([0, 1, 0, 16, 0, 0, 0, 0, 0, 0, 0, 1].join("\t"));
  waypoints.forEach((waypoint, index) => {
    const commandId =
      waypoint.commandIdOverride ?? commandIdFor(waypoint.itemType);
    lines.push(
      [
        index + 1,
        index === 0 ? 1 : 0,
        3,
        commandId,
        ...missionParameters(waypoint),
        1,
      ].join("\t"),
    );
  });
  return lines.join("\n");
}

export function parseQgc(
  raw: string,
): { waypoints: MissionWaypoint[]; defaultAltitude?: number } | null {
  try {
    const parsed = JSON.parse(raw) as {
      mission?: {
        items?: Array<{ command?: number; params?: number[] }>;
        defaultAltitude?: number;
      };
    };
    const items = parsed.mission?.items ?? [];
    const waypoints = items
      .map((item): MissionWaypoint | null => {
        const params = Array.isArray(item.params) ? item.params : [];
        const commandId = Number(item.command ?? 16);
        const itemType = itemTypeFor(commandId);
        const lat = Number(params[4]);
        const lon = Number(params[5]);
        const alt = Number(params[6]);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
          return null;
        }
        return {
          id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          latitude: lat,
          longitude: lon,
          altitude: Number.isFinite(alt) ? alt : 30,
          itemType,
          commandIdOverride: commandId,
          param3: Number(params[2] ?? 0),
          jumpTargetIndex: Number(params[0] ?? 1),
          jumpRepeatCount: Number(params[1] ?? 1),
          holdTimeS: Number(params[0] ?? 0),
          acceptanceRadiusM: Number(params[1] ?? 8),
          yawDeg: Number.isFinite(Number(params[3])) ? Number(params[3]) : null,
        };
      })
      .filter((waypoint): waypoint is MissionWaypoint => waypoint !== null);
    return { waypoints, defaultAltitude: parsed.mission?.defaultAltitude };
  } catch {
    return null;
  }
}

export function parseWpl(raw: string): MissionWaypoint[] {
  const lines = raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith("#"));
  if (lines.length === 0 || !lines[0].toUpperCase().startsWith("QGC WPL")) {
    return [];
  }
  const waypoints: MissionWaypoint[] = [];
  for (const line of lines.slice(1)) {
    const parts = line.split(/\s+/);
    if (parts.length < 12) {
      continue;
    }
    const seq = Number(parts[0]);
    if (!Number.isFinite(seq) || seq === 0) {
      continue;
    }
    const commandId = Number(parts[3]);
    const p1 = Number(parts[4]);
    const p2 = Number(parts[5]);
    const p3 = Number(parts[6]);
    const p4 = Number(parts[7]);
    const lat = Number(parts[8]);
    const lon = Number(parts[9]);
    const alt = Number(parts[10]);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      continue;
    }
    waypoints.push({
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      latitude: lat,
      longitude: lon,
      altitude: Number.isFinite(alt) ? alt : 30,
      itemType: itemTypeFor(commandId),
      commandIdOverride: Number.isFinite(commandId) ? commandId : null,
      param3: Number.isFinite(p3) ? p3 : 0,
      jumpTargetIndex: Number.isFinite(p1) ? p1 : 1,
      jumpRepeatCount: Number.isFinite(p2) ? p2 : 1,
      holdTimeS: Number.isFinite(p1) ? p1 : 0,
      acceptanceRadiusM: Number.isFinite(p2) ? p2 : 8,
      yawDeg: Number.isFinite(p4) ? p4 : null,
    });
  }
  return waypoints;
}
