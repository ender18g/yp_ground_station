import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: process.env.GITHUB_PAGES === "true" ? "/yp_ground_station/" : "/",
  plugins: [react()],
  // Preserve dynamic-import boundaries. Grouping React/Three manually pulls
  // shared React helpers into the 3D chunk and makes the map preload it.
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/tiles": "http://localhost:8000",
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
      },
    },
  },
});
