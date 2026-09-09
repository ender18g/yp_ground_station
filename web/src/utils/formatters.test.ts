import { describe, expect, it } from "vitest";
import { formatHeading, formatKnots, kmhToKnots, metersPerSecondToKnots } from "./formatters";

describe("display formatters", () => {
  it("normalizes and pads headings", () => {
    expect(formatHeading(360)).toBe("000");
    expect(formatHeading(-1)).toBe("359");
    expect(formatHeading(undefined)).toBe("---");
  });

  it("formats knot readouts", () => {
    expect(formatKnots(12.4)).toBe("12");
    expect(formatKnots(Number.NaN)).toBe("--");
    expect(kmhToKnots(1)).toBeCloseTo(0.539957, 6);
    expect(metersPerSecondToKnots(1)).toBeCloseTo(1.943844, 6);
  });
});
