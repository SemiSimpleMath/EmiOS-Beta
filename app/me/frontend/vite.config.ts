import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Production: Flask serves the build at /me, so the bundle's asset paths
// must be rooted at /me/ for absolute imports to resolve. In dev (npm run
// dev), Vite serves directly and the dev server proxies API calls to the
// Flask backend on port 8000.
export default defineConfig(({ command }) => ({
  base: command === "build" ? "/me/" : "/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/socket.io": {
        target: "http://localhost:8000",
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    emptyOutDir: true,
  },
}));
