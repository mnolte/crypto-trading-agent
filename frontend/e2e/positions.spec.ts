import { expect, test, type APIRequestContext } from "@playwright/test";

/**
 * 016 screenshots: the Positions & orders screen — the live account and a
 * shadow account — at desktop and phone width. The agent is paused first so
 * the numbers stay still for the shot; one cycle runs beforehand so the
 * page has real account and market data.
 */

async function localToken(request: APIRequestContext): Promise<string> {
  const res = await request.get("/api/session");
  const body = (await res.json()) as { token: string };
  return body.token;
}

test.beforeAll(async ({ request }) => {
  const token = await localToken(request);
  const headers = { "X-Local-Token": token };

  const snap = await request.get("/api/snapshot");
  const status = ((await snap.json()) as { status: string }).status;
  if (status === "killed") {
    await request.post("/api/agent/acknowledge-kill", { headers });
  }

  // One cycle so the account card and market data are real, then pause.
  const settingsResponse = await request.get("/api/settings");
  const settings = (await settingsResponse.json()) as Record<string, unknown>;
  settings.execution_mode = "manual";
  await request.put("/api/settings", { headers, data: settings });
  await request.post("/api/agent/run-now", { headers });
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
      { timeout: 60_000, intervals: [500, 1000, 2000] },
    )
    .toBe(true);
  await request.post("/api/agent/pause", { headers });

  // A stopped-or-running shadow gives the Live / Shadow switch a second
  // account to show; reuse one if the throwaway database already has it.
  const shadows = await request.get("/api/shadows");
  const list = ((await shadows.json()) as { shadows?: { name: string }[] })
    .shadows ?? [];
  if (!list.some((s) => s.name === "Shadow B")) {
    await request.post("/api/shadows", {
      headers,
      data: {
        name: "Shadow B",
        model: "gpt-4o-mini",
        prompt_version: "decision.v1",
        overrides: {},
      },
    });
  }
});

test("positions live account", async ({ page }, testInfo) => {
  await page.goto("/positions");
  await expect(page.locator(".shell-header")).toBeVisible();
  await expect(page.locator(".posscreen__switch")).toBeVisible();
  await page.waitForLoadState("networkidle");
  await page.screenshot({
    path: `../docs/screenshots/pr-16/positions-live-${testInfo.project.name}.png`,
    fullPage: true,
  });
  const scrollWidth = await page.evaluate("document.documentElement.scrollWidth");
  expect(scrollWidth).toBeLessThanOrEqual(
    (page.viewportSize()?.width ?? 1440) + 1,
  );
});

test("positions shadow account", async ({ page }, testInfo) => {
  await page.goto("/positions");
  await expect(page.locator(".shell-header")).toBeVisible();
  // beforeAll runs once per worker; a duplicate-name shadow is harmless —
  // either account shows the same empty shadow state.
  await page
    .getByRole("button", { name: "Shadow B", exact: true })
    .first()
    .click();
  await expect(page.locator(".posscreen__switch .posscreen__view--on")).toHaveText(
    "Shadow B",
  );
  await page.waitForLoadState("networkidle");
  await page.screenshot({
    path: `../docs/screenshots/pr-16/positions-shadow-b-${testInfo.project.name}.png`,
    fullPage: true,
  });
});
