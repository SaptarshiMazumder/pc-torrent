import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by FastAPI at /home (StaticFiles mount), so every asset URL is
// prefixed.  The dev proxy forwards API calls to a locally running serverV2.
export default defineConfig({
  base: "/home/",
  plugins: [react()],
  server: {
    proxy: {
      "/admin": "http://localhost:8080",
      "/me": "http://localhost:8080",
      "/app": "http://localhost:8080",
      "/render-groups": "http://localhost:8080",
      "/logs": "http://localhost:8080",
    },
  },
});
