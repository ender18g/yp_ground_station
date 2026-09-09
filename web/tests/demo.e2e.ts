import { expect, test } from "@playwright/test";

test("Pages demo opens without a server and renders telemetry and both planners", async ({ page }) => {
  const errors: string[] = [];
  const backendRequests: string[] = [];
  const plannerRequests: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (/\/(api|ws)\//.test(new URL(request.url()).pathname)) backendRequests.push(request.url());
    if (/(?:MissionPlannerMode-|WaypointPlanner-|three-|YP_CAD\.glb)/.test(request.url())) plannerRequests.push(request.url());
  });
  page.on("websocket", (socket) => backendRequests.push(socket.url()));

  // Public tile/weather availability is independent of the local demo. Keep CI
  // deterministic while letting every bundled asset load from the real preview.
  await page.route(/^https:\/\//, async (route) => {
    if (route.request().url().includes("api.open-meteo.com")) {
      await route.fulfill({ json: { current: { wind_speed_10m: 8, wind_direction_10m: 330 } } });
    } else {
      await route.fulfill({
        contentType: "image/png",
        body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=", "base64"),
      });
    }
  });

  await page.goto("./");
  await expect(page.getByTitle("Global Map", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign In", exact: true })).toHaveCount(0);
  await expect(page.locator(".leaflet-marker-pane img[alt=uav]")).toHaveCount(2);

  await page.getByTitle("Messages", { exact: true }).click();
  await expect(page.getByText("/vehicles/demo-uav-1/navsatfix", { exact: true }).first()).toBeVisible();
  await page.getByTitle("Close messages", { exact: true }).click();
  await page.getByTitle("Settings", { exact: true }).click();
  await expect(page.getByText("RTK Correction", { exact: true })).toBeVisible();
  await page.getByTitle("Settings", { exact: true }).click();

  // The global map must not download either planner or the 3D runtime/model.
  expect(plannerRequests).toEqual([]);

  await page.getByTitle("Mission Planner", { exact: true }).click();
  await expect(page.getByText("Waypoints: 0", { exact: true })).toBeVisible();
  await page.locator('input[type="file"]').setInputFiles({
    name: "mission.json", mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify({ waypoints: [
      { latitude: 38.984, longitude: -76.478, altitude: 0 },
      { latitude: 38.985, longitude: -76.479, altitude: 30 },
    ] })),
  });
  await expect(page.getByText("Waypoints: 2", { exact: true })).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export WPL", exact: true }).click();
  expect((await download).suggestedFilename()).toMatch(/\.waypoints$/);
  expect(plannerRequests.some((url) => url.includes("MissionPlannerMode-"))).toBe(true);
  expect(plannerRequests.filter((url) => /(?:WaypointPlanner-|three-|YP_CAD\.glb)/.test(url))).toEqual([]);

  const model = page.waitForResponse((response) => response.url().endsWith("/yp_ground_station/logos/YP_CAD.glb"));
  await page.getByTitle("Local Waypoint Planner", { exact: true }).click();
  expect((await model).status()).toBe(200);
  expect(plannerRequests.some((url) => url.includes("WaypointPlanner-"))).toBe(true);
  await expect(page.getByRole("heading", { name: "Lateral Planner (Top-Down)", exact: true })).toBeVisible();
  await expect(page.locator("canvas")).toBeVisible();
  await page.getByTitle("Global Map", { exact: true }).click();
  await expect(page.locator(".leaflet-marker-pane img[alt=uav]")).toHaveCount(2);
  expect(backendRequests).toEqual([]);
  expect(errors).toEqual([]);
});
