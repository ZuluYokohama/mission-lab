import { defineConfig, devices } from "@playwright/test";

// No webServer: each spec starts the Python feed itself (see e2e/feed.ts) so it controls speed,
// backfill and restarts.
export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  workers: 1,
  reporter: [["list"]],
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
