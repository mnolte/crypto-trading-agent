import { defineConfig } from "@playwright/test";

/**
 * Screenshots run against the real backend serving the built UI, so the
 * snapshot feed is genuine. A throwaway database keeps the repo's data/ clean.
 */
export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  timeout: 30_000,
  workers: 6,
  reporter: process.env.CI ? [["html"], ["line"]] : "list",
  use: {
    baseURL: "http://127.0.0.1:8011",
  },
  webServer: {
    // TA_DATA_SOURCE=sim keeps the run fully local and deterministic.
    command:
      "cd .. && env TA_DATA_SOURCE=sim TA_DB_PATH=${TA_DB_PATH:-/tmp/ta-e2e/agent.db} TA_DATA_DIR=${TA_DATA_DIR:-/tmp/ta-e2e} uv run uvicorn --app-dir backend app.main:app --host 127.0.0.1 --port 8011",
    url: "http://127.0.0.1:8011/api/health",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
  projects: [
    { name: "w1440", use: { viewport: { width: 1440, height: 900 } } },
    { name: "w1100", use: { viewport: { width: 1100, height: 900 } } },
    {
      name: "w390",
      // Phone-width coverage is for the screenshots spec; the other specs
      // assert behaviour, not layout.
      testMatch: [
        "screenshots.spec.ts",
        "pr15.spec.ts",
        "positions.spec.ts",
        "settings-sections.spec.ts",
      ],
      use: { viewport: { width: 390, height: 844 } },
    },
  ],
});
