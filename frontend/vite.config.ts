import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The UI stays same-origin: in dev, Vite proxies /api and /ws to the backend
// so no CORS headers are ever needed.
export default defineConfig({
  plugins: [react()],
  test: {
    // Unit tests live next to the code; e2e/*.spec.ts is Playwright's.
    include: ["src/**/*.test.{ts,tsx}"],
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
});
