import { describe, expect, it } from "vitest";
import {
  commandIdFor,
  parseQgc,
  parseWpl,
  toQgcPlan,
  toWpl,
  type MissionItemType,
  type MissionWaypoint,
} from "./missionFiles";

const waypoint = (changes: Partial<MissionWaypoint> = {}): MissionWaypoint => ({
  id: "test-waypoint",
  latitude: 38.9822,
  longitude: -76.4819,
  altitude: 0,
  itemType: "waypoint",
  commandIdOverride: null,
  param3: 7,
  jumpTargetIndex: 2,
  jumpRepeatCount: 3,
  holdTimeS: 5,
  acceptanceRadiusM: 8,
  yawDeg: 90,
  ...changes,
});

describe.each([
  [
    "QGroundControl",
    (items: MissionWaypoint[]) =>
      parseQgc(JSON.stringify(toQgcPlan(items, 35)))!.waypoints,
  ],
  ["Mission Planner WPL", (items: MissionWaypoint[]) => parseWpl(toWpl(items))],
] as const)("%s mission files", (_name, roundTrip) => {
  it("preserves commands, coordinates, zero altitude, and parameters", () => {
    const types: MissionItemType[] = [
      "waypoint",
      "takeoff",
      "loiter_time",
      "land",
      "rtl",
      "do_jump",
    ];
    const imported = roundTrip(types.map((itemType) => waypoint({ itemType })));
    expect(imported).toHaveLength(types.length);
    imported.forEach((item, index) => {
      expect(item).toMatchObject({
        itemType: types[index],
        commandIdOverride: commandIdFor(types[index]),
        latitude: 38.9822,
        longitude: -76.4819,
        altitude: 0,
        param3: 7,
        yawDeg: 90,
      });
      expect(item).toMatchObject(
        item.itemType === "do_jump"
          ? { jumpTargetIndex: 2, jumpRepeatCount: 3 }
          : { holdTimeS: 5, acceptanceRadiusM: 8 },
      );
    });
  });

  it("preserves unknown MAV_CMD overrides and negative altitude", () => {
    expect(
      roundTrip([waypoint({ commandIdOverride: 179, altitude: -8 })])[0],
    ).toMatchObject({
      commandIdOverride: 179,
      altitude: -8,
      holdTimeS: 5,
      acceptanceRadiusM: 8,
    });
  });

  it("uses jump parameters when the override is DO_JUMP", () => {
    expect(roundTrip([waypoint({ commandIdOverride: 177 })])[0]).toMatchObject({
      itemType: "do_jump",
      jumpTargetIndex: 2,
      jumpRepeatCount: 3,
    });
  });
});

it("preserves QGC default altitude", () => {
  expect(
    parseQgc(JSON.stringify(toQgcPlan([waypoint()], 42)))?.defaultAltitude,
  ).toBe(42);
});

it("ignores WPL home rows, comments, and malformed sequences", () => {
  const row = "1 0 3 16 5 8 7 90 38.9822 -76.4819 0 1";
  const file = `QGC WPL 110\n# generated mission\n0 1 0 16 0 0 0 0 0 0 0 1\n${row.replace(/^1/, "invalid")}\n${row}\nshort row`;
  expect(parseWpl(file)).toHaveLength(1);
  expect(parseWpl(file)[0]).toMatchObject({
    latitude: 38.9822,
    longitude: -76.4819,
    altitude: 0,
  });
});

it("rejects unrelated files and ignores invalid coordinates", () => {
  expect(parseWpl("not a mission")).toEqual([]);
  expect(parseQgc("not json")).toBeNull();
  expect(
    parseQgc(
      JSON.stringify({ mission: { items: [{ command: 16, params: [] }] } }),
    )?.waypoints,
  ).toEqual([]);
});
