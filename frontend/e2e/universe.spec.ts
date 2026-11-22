import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * 026 Universe screen: sidebar navigation, filters, the in-row activate and
 * deactivate confirmations, the reduced Settings → Markets section, the
 * failed-check banner and a running backfill — plus the item's screenshots.
 * The agent is paused so numbers stay still; one cycle runs first so the
 * sim market has real candles and an eligibility result. Tests run in
 * order: the plain screenshots come before the seeded states and the
 * shared-database mutations.
 */

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const DB = process.env.TA_DB_PATH ?? "/tmp/ta-e2e/agent.db";
const SHOTS = path.resolve(REPO, "docs/screenshots/026");

async function localToken(request: APIRequestContext): Promise<string> {
  const res = await request.get("/api/session");
  const body = (await res.json()) as { token: string };
  return body.token;
}

function seed(...args: string[]) {
  execFileSync("uv", ["run", "python", "e2e/seed-universe.py", ...args], {
    cwd: path.resolve(REPO, "frontend"),
    env: { ...process.env, TA_DB_PATH: DB, PYTHONPATH: "../backend" },
  });
}

function marketRow(page: Page, pair: string) {
  return page.getByRole("row").filter({ hasText: pair });
}

test.describe("Universe screen", () => {
  // Serial: the projects share one throwaway database, so the
  // seeded states and the plain screenshots must not interleave.
  test.describe.configure({ mode: "serial" });

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
      data: { settings, reason: "e2e: manual mode for universe" },
    });
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
  });

  test("sidebar opens the Universe screen", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("link", { name: "Universe" }).first().click();
    await expect(page).toHaveURL(/\/universe/);
    await expect(
      page.getByRole("heading", { name: "Universe", level: 1 }),
    ).toBeVisible();
    await expect(page.getByTestId("universe-tiles")).toBeVisible();
    await expect(
      page.getByText("Every Bitvavo EUR market is checked"),
    ).toBeVisible();
  });

  test("tiles, funnel, sync card and market table render", async ({ page }) => {
    await page.goto("/universe");
    await expect(page.getByTestId("universe-tiles")).toBeVisible();
    await expect(
      page.getByTestId("universe-tiles").getByText("EUR markets"),
    ).toBeVisible();
    await expect(page.getByText("Today's check")).toBeVisible();
    await expect(page.getByText("Data sync")).toBeVisible();
    await expect(page.getByText("How the check works")).toBeVisible();
    // The sim universe is small: every group fits, no fold rows.
    await expect(
      page.getByRole("row").filter({ hasText: "BTC-EUR" }),
    ).toBeVisible();
  });

  test("universe screenshots", async ({ page }, testInfo) => {
    await page.goto("/universe");
    await expect(page.getByTestId("universe-tiles")).toBeVisible();
    await page.waitForLoadState("networkidle");
    if (testInfo.project.name === "w1440") {
      await page.screenshot({ path: path.join(SHOTS, "Universe.png") });
    } else if (testInfo.project.name === "w390") {
      await page.screenshot({ path: path.join(SHOTS, "Universe-390.png") });
    }
  });

  test("filters persist in the URL and search narrows the table", async ({
    page,
  }) => {
    await page.goto("/universe?filter=eligible");
    const seg = page.getByRole("group", { name: "Filter markets" });
    await expect(seg.getByRole("button", { name: /Eligible/ })).toHaveClass(
      /seg__btn--on/,
    );
    // Eligible filter shows its group in full: no fold rows.
    await expect(
      page.getByRole("button", { name: /more (active|eligible|excluded)/ }),
    ).toHaveCount(0);

    await page.goto("/universe");
    await page.getByLabel("Find a market").fill("btc");
    await expect(
      page.getByRole("row").filter({ hasText: "BTC-EUR" }),
    ).toHaveCount(1);
    await expect(
      page.getByRole("row").filter({ hasText: "ETH-EUR" }),
    ).toHaveCount(0);
  });

  test("activate asks in the row, then the market warms up to active", async ({
    page,
    request,
  }) => {
    test.skip(test.info().project.name !== "w1440", "mutates the shared db once");
    // The sim backfill loads ~1,400 candles; give it room on a slow runner.
    test.setTimeout(120_000);
    const token = await localToken(request);
    const headers = { "X-Local-Token": token };

    // Free one market so an eligible, non-active row exists.
    const deact = await request.put("/api/markets/AVAX-EUR", {
      headers,
      data: { active: false },
    });
    expect(deact.ok()).toBeTruthy();

    await page.goto("/universe?filter=eligible");
    await page.getByLabel("Find a market").fill("AVAX");
    const row = marketRow(page, "AVAX-EUR");
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "Activate" }).click();

    // In-row confirmation: the looser tag, candles and the calls comparison.
    const dialog = page.getByRole("alertdialog", { name: "Activate AVAX-EUR?" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText("looser")).toBeVisible();
    await expect(
      dialog.getByText(/Loads 800 1h, 300 4h and 300 1d candles/),
    ).toBeVisible();
    await expect(dialog.getByText(/of the 1,000 budget/)).toBeVisible();
    await page.screenshot({
      path: path.join(SHOTS, "universe-activate-confirm.png"),
    });

    await dialog
      .getByRole("button", { name: "Activate", exact: true })
      .click();
    // The row leaves the eligible filter (it is active now). A search
    // bypasses the fold, so the row stays findable among many actives.
    await page.goto("/universe");
    await page.getByLabel("Find a market").fill("AVAX");
    const activeRow = marketRow(page, "AVAX-EUR");
    await expect(
      activeRow.getByText("active", { exact: true }).first(),
    ).toBeVisible({ timeout: 90_000 });
  });

  test("deactivate asks in the row", async ({ page, request }) => {
    test.skip(test.info().project.name !== "w1440", "mutates the shared db once");
    const token = await localToken(request);
    const headers = { "X-Local-Token": token };
    await request.put("/api/markets/DOGE-EUR", {
      headers,
      data: { active: false },
    });
    await request.put("/api/markets/DOGE-EUR", {
      headers,
      data: { active: true, confirm: true },
    });

    await page.goto("/universe");
    // Search keeps the row visible past the active fold.
    await page.getByLabel("Find a market").fill("DOGE");
    const row = marketRow(page, "DOGE-EUR");
    await row.getByRole("button", { name: "Deactivate" }).click();
    const dialog = page.getByRole("alertdialog", { name: "Deactivate DOGE-EUR?" });
    await expect(dialog).toBeVisible();
    await expect(
      dialog.getByText(/No new entries; an open position is still managed/),
    ).toBeVisible();
    await dialog
      .getByRole("button", { name: "Deactivate", exact: true })
      .click();
    // Deactivated and still eligible: the row now offers Activate again.
    await page.goto("/universe?filter=eligible");
    await page.getByLabel("Find a market").fill("DOGE");
    await expect(marketRow(page, "DOGE-EUR")).toBeVisible();
  });

  test("Settings → Markets keeps thresholds and links to Universe", async ({
    page,
  }) => {
    await page.goto("/settings/markets");
    await expect(
      page.getByRole("link", { name: "Open Universe" }),
    ).toHaveAttribute("href", "/universe");
    await expect(page.getByText(/\d+ active:/)).toBeVisible();
    // The old market table and its search are gone.
    await expect(page.getByLabel("Search markets")).toHaveCount(0);
    await expect(page.getByText("30d vol/day")).toHaveCount(0);
    // The data error share lives in the Advanced fold.
    await page.getByText("Advanced").first().click();
    await expect(page.getByText("Data error share")).toBeVisible();
    if (test.info().project.name === "w1440") {
      await page.screenshot({
        path: path.join(SHOTS, "settings-markets.png"),
        fullPage: true,
      });
    }
  });

  test("a failed check turns the banner amber", async ({ page }) => {
    test.skip(test.info().project.name !== "w1440", "screenshot once");
    seed("failed-check");
    await page.goto("/universe");
    await expect(page.getByText(/The check at .* failed/)).toBeVisible();
    await expect(page.getByText(/Using the result of/)).toBeVisible();
    await page.screenshot({
      path: path.join(SHOTS, "universe-check-failed.png"),
    });
  });

  test("Data sync opens itself while a backfill runs", async ({ page }) => {
    test.skip(test.info().project.name !== "w1440", "screenshot once");
    seed("running-backfill");
    await page.goto("/universe");
    await expect(page.getByText("Loading history · SHOT-EUR")).toBeVisible({
      timeout: 15_000,
    });
    await page.screenshot({
      path: path.join(SHOTS, "universe-sync-running.png"),
    });
  });

  test("the page does not scroll sideways at phone width", async ({ page }) => {
    test.skip(test.info().project.name !== "w390", "phone project only");
    await page.goto("/universe");
    await expect(page.getByTestId("universe-tiles")).toBeVisible();
    const width = await page.evaluate(
      "document.scrollingElement?.scrollWidth ?? document.documentElement.scrollWidth",
    );
    expect(width).toBeLessThanOrEqual(390);
  });
});
