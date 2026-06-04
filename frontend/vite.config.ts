import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies API + WebSocket calls to the FastAPI backend so the frontend can use
// relative paths (no CORS juggling in dev). Backend is expected at 127.0.0.1:8001.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8001", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8001", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8001", ws: true },
    },
  },
});
