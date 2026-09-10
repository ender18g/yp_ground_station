import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:4174/" });

const image = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=", "base64");

async function mockLiveStation(page: Page, permissions: string[], initiallyAuthenticated = false) {
  let authenticated = initiallyAuthenticated;
  const sockets: WebSocketRoute[] = [];
  const errors: string[] = [];
  const unknownRequests: string[] = [];
  let logoutRequests = 0;
  page.on("pageerror", (error) => errors.push(error.message));

  await page.route(/^https:\/\//, (route) => route.request().url().includes("api.open-meteo.com")
    ? route.fulfill({ json: { current: { wind_speed_10m: 8, wind_direction_10m: 330 } } })
    : route.fulfill({ contentType: "image/png", body: image }));
  await page.route("**/tiles/**", (route) => route.fulfill({ contentType: "image/png", body: image }));
  await page.routeWebSocket("**/ws/ui", (socket) => {
    sockets.push(socket);
    socket.send(JSON.stringify({ op: "snapshot", vehicles: [], waypoints: [], sar_patterns: {}, mission_plans: {} }));
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/auth/me") {
      await route.fulfill(authenticated ? { json: {
        username: "operator", active: true, permissions, created_at: null, last_login: null,
      } } : { status: 401, json: { error: "Authentication required" } });
    } else if (path === "/api/auth/login") {
      const payload = request.postDataJSON();
      authenticated = payload.username === "operator" && payload.password === "password";
      await route.fulfill(authenticated ? { json: { ok: true } } : { status: 401, json: { error: "Invalid credentials" } });
    } else if (path === "/api/auth/logout") {
      authenticated = false;
      logoutRequests += 1;
      await route.fulfill({ json: { ok: true } });
    } else if (path === "/api/settings") {
      await route.fulfill(request.method() === "PUT" && !permissions.includes("manage_settings")
        ? { status: 403, json: { error: "Insufficient permissions" } }
        : { json: { message_retention_seconds: 600, message_cleanup_interval_seconds: 30, influx_max_write_hz: 5, tile_max_cache_age_seconds: 3600 } });
    } else if (path === "/api/deconfliction/settings") {
      await route.fulfill(request.method() === "PUT" && !permissions.includes("manage_settings")
        ? { status: 403, json: { error: "Insufficient permissions" } }
        : { json: { enabled: false, global_radius_m: 10, radius_per_type: {}, orbit_radius_m: 50, max_pause_duration_s: 300 } });
    } else if (path === "/api/sitl") {
      await route.fulfill({ json: { bridges: [] } });
    } else {
      unknownRequests.push(path);
      await route.fulfill({ status: 404, json: { error: "Unexpected request" } });
    }
  });

  return { sockets, errors, unknownRequests, logoutRequests: () => logoutRequests };
}

test("live session requires login, retains restricted permissions, and logs out", async ({ page }) => {
  const station = await mockLiveStation(page, ["view_telemetry"]);
  await page.goto("./");
  await expect(page.getByRole("button", { name: "Sign In", exact: true })).toBeVisible();
  await expect(page.getByTitle("Global Map", { exact: true })).toHaveCount(0);
  expect(station.sockets).toHaveLength(0);

  await page.getByLabel("Username", { exact: true }).fill("operator");
  await page.getByLabel("Password", { exact: true }).fill("incorrect");
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page.getByText("Invalid credentials", { exact: true })).toBeVisible();
  await expect(page.getByTitle("Global Map", { exact: true })).toHaveCount(0);

  await page.getByLabel("Password", { exact: true }).fill("password");
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page.getByTitle("Global Map", { exact: true })).toBeVisible();
  await expect(page.getByTitle("User Management", { exact: true })).toHaveCount(0);
  await expect(page.getByTitle("Save Flight Log", { exact: true })).toHaveCount(0);
  await expect.poll(() => station.sockets.length).toBeGreaterThan(0);
  await expect(page.locator(".leaflet-marker-pane img[alt=uav]")).toHaveCount(0);

  await page.getByTitle("Logout", { exact: true }).click();
  await expect(page.getByRole("button", { name: "Sign In", exact: true })).toBeVisible();
  await expect.poll(station.logoutRequests).toBe(1);
  await page.reload();
  await expect(page.getByRole("button", { name: "Sign In", exact: true })).toBeVisible();
  expect(station.errors).toEqual([]);
  expect(station.unknownRequests).toEqual([]);
});

test("live session retains admin permissions and returns to login when authentication expires", async ({ page }) => {
  const station = await mockLiveStation(page, ["manage_settings", "manage_users"], true);
  await page.goto("./");
  await expect(page.getByTitle("Global Map", { exact: true })).toBeVisible();
  await expect(page.getByTitle("User Management", { exact: true })).toBeVisible();
  await expect(page.getByTitle("Save Flight Log", { exact: true })).toBeVisible();
  await expect.poll(() => station.sockets.length).toBeGreaterThan(0);

  const socket = station.sockets[station.sockets.length - 1];
  socket.close({ code: 4001, reason: "Unauthorized" });
  await expect(page.getByRole("button", { name: "Sign In", exact: true })).toBeVisible();
  await expect(page.getByTitle("Global Map", { exact: true })).toHaveCount(0);
  await expect.poll(station.logoutRequests).toBe(1);
  expect(station.errors).toEqual([]);
  expect(station.unknownRequests).toEqual([]);
});
