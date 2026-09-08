export function kmhToKnots(speedKmh: number): number {
  return speedKmh * 0.539957;
}

export function metersPerSecondToKnots(speedMps: number): number {
  return speedMps * 1.943844;
}

export function formatHeading(directionDeg?: number): string {
  if (typeof directionDeg !== "number" || !Number.isFinite(directionDeg)) return "---";
  return String(((Math.round(directionDeg) % 360) + 360) % 360).padStart(3, "0");
}

export function formatKnots(speedKts?: number): string {
  return typeof speedKts === "number" && Number.isFinite(speedKts) ? String(Math.round(speedKts)) : "--";
}
