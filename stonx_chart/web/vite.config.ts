import { defineConfig } from "vitest/config";

export default defineConfig({
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8765" },
    },
  },
  build: {
    outDir: "dist",
  },
  test: {
    include: ["tests/**/*.test.ts"],
    environment: "node",
  },
});
