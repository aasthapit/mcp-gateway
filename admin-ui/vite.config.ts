/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";
// @ts-expect-error plain ESM helper shared with server.mjs
import { runtimeConfig } from "./runtime-config.mjs";

/** Dev server: serve /config.json from ADMIN_UI_* like server.mjs does in the container. */
function configJson(): Plugin {
  return {
    name: "admin-ui-config-json",
    configureServer(server) {
      server.middlewares.use("/config.json", (_req, res) => {
        res.setHeader("content-type", "application/json");
        res.setHeader("cache-control", "no-store");
        res.end(JSON.stringify(runtimeConfig()));
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), configJson()],
  server: {
    host: "127.0.0.1",
    port: Number(process.env.ADMIN_UI_PORT ?? 3000),
    strictPort: true,
    // The forms read enums straight from ../schemas (single source of truth).
    fs: { allow: [".", "../schemas"] },
  },
  build: { outDir: "dist", sourcemap: true },
  test: {
    environment: "jsdom",
    include: ["test/**/*.test.{ts,tsx}"],
    setupFiles: ["test/setup.ts"],
  },
});
