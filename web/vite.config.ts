import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the Python backend runs on :8000; Vite proxies the WebSocket to it.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
});
