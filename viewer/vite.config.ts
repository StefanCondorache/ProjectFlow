import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The built viewer ships inside the Python package; `flowmap` serves it.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../src/flowmap/web", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
