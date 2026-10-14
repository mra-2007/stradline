import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built bundle is served by the Strandline API at "/", so every request the
// app makes is relative. In `npm run dev` the same relative paths are proxied
// to the API on :8000, which keeps one code path for both.
const api = "http://127.0.0.1:8000";

export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/health": api,
      "/preflight": api,
      "/runs": api,
    },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        manualChunks: {
          maplibre: ["maplibre-gl"],
          three: ["three", "@react-three/fiber"],
        },
      },
    },
  },
});
