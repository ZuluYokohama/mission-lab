import { defineConfig } from "vitest/config";

export default defineConfig({
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8765" },
    },
  },
  build: {
    outDir: "dist",
    // Keep the bundled libraries' license headers (lightweight-charts is Apache-2.0) in the output.
    rolldownOptions: { output: { comments: { legal: true } } },
  },
  test: {
    include: ["tests/**/*.test.ts"],
    environment: "node",
  },
});
