import { describe, expect, it } from "vitest";
import { bearingDegrees, destinationPoint, haversineMeters } from "./geo";

describe("geo utilities", () => {
  it("returns zero distance for identical points", () => {
    expect(haversineMeters(38.98, -76.48, 38.98, -76.48)).toBe(0);
  });

  it("calculates cardinal bearings", () => {
    expect(bearingDegrees(0, 0, 1, 0)).toBeCloseTo(0, 5);
    expect(bearingDegrees(0, 0, 0, 1)).toBeCloseTo(90, 5);
  });

  it("projects a point by distance and bearing", () => {
    const point = destinationPoint(0, 0, 0, 1000);
    expect(point.latitude).toBeCloseTo(0.008993, 5);
    expect(point.longitude).toBeCloseTo(0, 6);
  });
});
