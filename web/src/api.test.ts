import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchSettings, login, updateSettings, updateDeconflictionSettings } from "./api";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("authenticated API requests", () => {
  it("includes credentials when logging in so the auth cookie is stored", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    );

    await login("admin", "admin");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/login",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("includes credentials on authenticated API calls", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ message_retention_seconds: 60 }), { status: 200 }),
    );

    await fetchSettings();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("sends settings as authenticated JSON PUT requests", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) =>
      new Response(init?.body as string, { status: 200 }),
    );
    expect(await updateSettings({ rtb_altitude_m: 0 })).toEqual({ rtb_altitude_m: 0 });
    expect(fetchMock).toHaveBeenLastCalledWith("/api/settings", {
      credentials: "include", method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rtb_altitude_m: 0 }),
    });
    expect(await updateDeconflictionSettings({ enabled: false })).toEqual({ enabled: false });
    expect(fetchMock).toHaveBeenLastCalledWith("/api/deconfliction/settings", expect.objectContaining({ method: "PUT" }));
  });

  it("preserves useful errors when a settings request is rejected", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 403 }));
    await expect(updateSettings({ rtb_altitude_m: 30 })).rejects.toThrow("settings update failed: 403");
    await expect(updateDeconflictionSettings({ enabled: true })).rejects.toThrow("deconfliction settings update failed: 403");
  });
});
