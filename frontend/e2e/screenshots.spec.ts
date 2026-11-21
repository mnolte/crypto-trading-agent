import { expect, test, type APIRequestContext } from "@playwright/test";

const PR_DIR = process.env.SCREENSHOT_DIR ?? "pr-15";

const PAGES = [
  { path: "/", name: "dashboard" },
  { path: "/indicators", name: "indicators" },
  { path: "/performance", name: "performance" },
  { path: "/insights", name: "insights" },
  { path: "/what-if", name: "what-if" },
  { path: "/reviews", name: "reviews" },
  { path: "/settings", name: "settings" },
  { path: "/inspector", name: "inspector" },
  { path: "/logs", name: "logs" },
  { path: "/readiness", name: "readiness" },
  { path: "/dev/components", name: "components" },
];

async function localToken(request: APIRequestContext): Promise<string> {
  const res = await request.get("/api/session");
  const body = (await res.json()) as { token: string };
  return body.token;
}

/**
 * Seed the throwaway database: run one full cycle against the simulated
 * market so candles, decisions, indicators and the equity curve exist before
 * screenshots. The engine is paused again afterwards for stable shots.
 */
test.beforeAll(async ({ request }) => {
  const token = await localToken(request);
  const headers = { "X-Local-Token": token };

  const snap = await request.get("/api/snapshot");
  const status = ((await snap.json()) as { status: string }).status;
  if (status === "killed") {
    await request.post("/api/agent/acknowledge-kill", { headers });
  }

  const settingsResponse = await request.get("/api/settings");
  const settings = (await settingsResponse.json()) as Record<string, unknown>;
  settings.execution_mode = "manual";
  await request.put("/api/settings", {
    headers,
    data: { settings, reason: "e2e: manual mode for screenshots" },
  });
  await request.post("/api/agent/run-now", { headers });

  // Wait for the cycle to finish (candles fetched + decisions written).
  await expect
    .poll(
      async () => {
        const res = await request.get("/api/snapshot");
        const body = (await res.json()) as {
          cycle: number;
          cycle_in_progress: boolean;
        };
        return body.cycle >= 1 && !body.cycle_in_progress;
      },
      { timeout: 25_000, intervals: [500, 1000, 1000, 2000] },
    )
    .toBe(true);

  await request.post("/api/agent/pause", { headers });
});

for (const { path, name } of PAGES) {
  test(`screenshot ${name}`, async ({ page }, testInfo) => {
    await page.goto(path);
    await expect(page.locator(".shell-header")).toBeVisible();
    // Wait for the snapshot feed and the screen's own data.
    await expect(page.locator(".shell-header .status-pill")).not.toHaveText(
      /Disconnected/,
      { timeout: 10_000 },
    );
    // Let the screen's own queries settle (websockets don't block networkidle).
    await page.waitForLoadState("networkidle");
    await page.screenshot({
      path: `../docs/screenshots/${PR_DIR}/${name}-${testInfo.project.name}.png`,
      fullPage: name === "components" || name === "settings",
    });
    // No screen scrolls sideways at phone width.
    if ((page.viewportSize()?.width ?? 0) <= 400) {
      const scrollWidth = await page.evaluate(
        "document.documentElement.scrollWidth",
      );
      expect(scrollWidth).toBeLessThanOrEqual(391);
    }
  });
}

test("ask panel opens over the dashboard", async ({ page }, testInfo) => {
  await page.goto("/");
  await page.locator(".shell-header").getByRole("button", { name: "Ask" }).click();
  const panel = page.locator(".slide-panel--side");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("Read-only");
  await page.screenshot({
    path: `../docs/screenshots/${PR_DIR}/ask-${testInfo.project.name}.png`,
  });
});

test("decision drawer opens from the dashboard", async ({ page }, testInfo) => {
  await page.goto("/");
  const open = page.getByRole("button", { name: "Open full decision" });
  await expect(open).toBeVisible({ timeout: 10_000 });
  await open.click();
  const drawer = page.locator(".slide-panel--drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText(/Safety gates|Agent reasoning/);
  await page.screenshot({
    path: `../docs/screenshots/${PR_DIR}/decision-drawer-${testInfo.project.name}.png`,
  });
});

test("stop and flatten asks for confirmation", async ({ page }, testInfo) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Stop & flatten" }).click();
  const dialog = page.getByRole("dialog", { name: "Close all positions and pause?" });
  await expect(dialog).toBeVisible();
  await page.screenshot({
    path: `../docs/screenshots/${PR_DIR}/flatten-dialog-${testInfo.project.name}.png`,
  });
});

test("disconnected pill when the snapshot socket is down", async ({ page }, testInfo) => {
  // Kill the /ws socket as soon as it opens; the pill must show Disconnected,
  // never a stale "Paused".
  await page.routeWebSocket("**/ws", (ws) => {
    ws.close();
  });
  await page.goto("/");
  const pill = page.locator(".shell-header .status-pill");
  await expect(pill).toHaveText(/Disconnected/);
  await expect(pill).toHaveAttribute("title", /Last known:/);
  await page.screenshot({
    path: `../docs/screenshots/${PR_DIR}/disconnected-${testInfo.project.name}.png`,
  });
});
