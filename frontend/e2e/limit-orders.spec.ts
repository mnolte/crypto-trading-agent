import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type APIRequestContext } from "@playwright/test";

/**
 * PR 12 screenshots: the decision-drawer footer for a resting, filled and
 * unfilled limit entry. The drawer data is seeded straight into the
 * throwaway e2e database — a real resting order would need the 30-second
 * watcher and is timing-fragile.
 */

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const DB = process.env.TA_DB_PATH ?? "/tmp/ta-e2e/agent.db";

async function localToken(request: APIRequestContext): Promise<string> {
  const res = await request.get("/api/session");
  const body = (await res.json()) as { token: string };
  return body.token;
}

test.setTimeout(60_000);

let headers: Record<string, string>;
let originalSettings: unknown;

test.beforeAll(async ({ request }) => {
  const token = await localToken(request);
  headers = { "X-Local-Token": token };
  // Pause the agent so the watcher cannot cancel the seeded open order,
  // then wait out any in-flight cycle — direct SQLite writes must not
  // collide with the engine's commits (locked-database stalls).
  await request.post("/api/agent/pause", { headers });
  await expect
    .poll(
      async () => {
        const res = await request.get("/api/snapshot");
        return ((await res.json()) as { cycle_in_progress: boolean })
          .cycle_in_progress;
      },
      { timeout: 50_000, intervals: [500, 1000, 2000] },
    )
    .toBe(false);
  // The seeded open order rests as a post-only limit. The watcher cancels
  // any open entry order under market entries, so switch the runtime to
  // limit entries before seeding; afterAll restores the original settings.
  const res = await request.get("/api/settings");
  originalSettings = await res.json();
  const updated = { ...(originalSettings as Record<string, unknown>) };
  updated.paper = {
    ...(updated.paper as Record<string, unknown>),
    entry_orders: "limit",
  };
  await request.put("/api/settings", { headers, data: updated });
  seed();
});

function seed(...ids: string[]) {
  execFileSync("uv", ["run", "python", "e2e/seed-limit-orders.py", ...ids], {
    cwd: path.resolve(REPO, "frontend"),
    env: { ...process.env, TA_DB_PATH: DB, PYTHONPATH: "../backend" },
  });
}

test.afterAll(async ({ request }) => {
  if (headers && originalSettings !== undefined) {
    await request.put("/api/settings", { headers, data: originalSettings });
  }
});

const STATES = [
  {
    id: "e2e-limit-wait",
    name: "order-waiting",
    limit: "Limit 30,000.00",
    text: "Waiting for fill",
  },
  { id: "e2e-limit-fill", name: "order-filled", limit: "Limit 75,170.00", text: "Filled" },
  { id: "e2e-limit-unf", name: "order-unfilled", limit: "Limit 75,170.00", text: "Unfilled" },
];

for (const { id, name, limit, text } of STATES) {
  test(`decision drawer limit order: ${name}`, async ({ page }, testInfo) => {
    const drawer = page.locator(".slide-panel--drawer");
    // A resting limit order dies at its pair's next cycle, and the parallel
    // screenshot spec fires run-now cycles: re-seed and retry until the
    // drawer catches the order while it is still open.
    await expect(async () => {
      if (id === "e2e-limit-wait") seed(id);
      await page.goto(`/?decision=${id}`);
      await expect(drawer).toBeVisible({ timeout: 10_000 });
      await expect(drawer).toContainText(limit);
      await expect(drawer).toContainText(text);
    }).toPass({ timeout: 55_000 });
    await page.screenshot({
      path: `screenshots/${name}-${testInfo.project.name}.png`,
    });
  });
}
