import { expect, test, type Page } from "@playwright/test";
import { fileURLToPath } from "url";
import path from "path";

const PR_DIR = process.env.SCREENSHOT_DIR ?? "pr-15";
const REPO = path.resolve(fileURLToPath(import.meta.url), "../../..");

/**
 * PR 15 shots: the Logs trace view, the error pages, and the record
 * not-found pages — at both required widths. The webServer fixture
 * (playwright.config.ts) runs the seeded sim backend.
 */

async function shot(page: Page, name: string, testInfo: { project: { name: string } }) {
  await page.waitForLoadState("networkidle");
  await page.screenshot({
    path: `../docs/screenshots/${PR_DIR}/${name}-${testInfo.project.name}.png`,
  });
  if ((page.viewportSize()?.width ?? 0) <= 400) {
    const scrollWidth = await page.evaluate(
      "document.documentElement.scrollWidth",
    );
    expect(scrollWidth).toBeLessThanOrEqual(400);
  }
}

test("logs trace view", async ({ page }, testInfo) => {
  await page.goto("/logs?cycle_id=c-0001");
  await expect(page.getByText(/Every line of cycle/)).toBeVisible();
  await shot(page, "logs-trace", testInfo);
});

test("unknown route shows 'There is no page here'", async ({ page }, testInfo) => {
  await page.goto("/this-page-does-not-exist");
  await expect(page.getByRole("heading", { name: "There is no page here" })).toBeVisible();
  await shot(page, "error-404-page", testInfo);
});

test("app not answering on first load", async ({ page }, testInfo) => {
  // Kill every API call before the app boots: the health gate shows the
  // "The app isn't answering" page.
  await page.route("**/api/**", (r) => r.abort());
  await page.goto("/");
  await expect(page.getByText("The app isn't answering")).toBeVisible({
    timeout: 15_000,
  });
  await shot(page, "error-not-answering", testInfo);
});

test("unknown decision id shows the record not-found page", async ({ page }, testInfo) => {
  await page.goto("/?decision=d-doesnotexist");
  await expect(
    page.getByText(/Decision d-doesnotexist doesn't exist/),
  ).toBeVisible({ timeout: 15_000 });
  await shot(page, "error-404-record", testInfo);
});

// The static reverse-proxy pages must be fully self-contained: no
// requests leave them, and they render with JavaScript disabled.
for (const file of ["app-down", "updating"]) {
  test(`static page ${file} makes no requests and needs no JS`, async ({
    browser,
  }) => {
    const context = await browser.newContext({ javaScriptEnabled: false });
    const page = await context.newPage();
    const requests: string[] = [];
    page.on("request", (r) => requests.push(r.url()));
    await page.goto(`file://${path.join(REPO, "deploy/errors", file + ".html")}`);
    await expect(page.locator("body")).toBeVisible();
    // Only the file:// document itself — no fonts, images, stylesheets or
    // API calls may leave the page.
    expect(requests.filter((u) => !u.startsWith("file://"))).toEqual([]);
    await context.close();
  });
}
