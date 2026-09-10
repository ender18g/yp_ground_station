import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.e2e.ts",
  fullyParallel: true,
  use: {
    baseURL: "http://127.0.0.1:4173/yp_ground_station/",
    viewport: { width: 1440, height: 900 },
    serviceWorkers: "block",
    launchOptions: { args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] },
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "npm run build:demo && npm run preview -- --host 127.0.0.1 --port 4173 --strictPort",
      env: { GITHUB_PAGES: "true" },
      url: "http://127.0.0.1:4173/yp_ground_station/",
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 4174 --strictPort",
      env: { GITHUB_PAGES: "false", VITE_STATIC_DEMO: "false" },
      url: "http://127.0.0.1:4174/",
      reuseExistingServer: false,
    },
  ],
});
