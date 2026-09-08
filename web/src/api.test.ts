import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchSettings, login } from "./api";

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
});
