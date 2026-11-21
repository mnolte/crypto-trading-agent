import { expect, test } from "@playwright/test";

const SHOT_DIR = "../docs/screenshots/025";

/** Item 025: sectioned Settings — nav, drafts, save bar, confirm, history. */

test("sections navigate and keep the draft", async ({ page }) => {
  await page.goto("/settings");
  // Plain /settings opens the last section (risk by default).
  await expect(page).toHaveURL(/\/settings\/(risk|markets|execution|exits|rules|broker|model|shadows|notify|display|keys|ready|schedule|history|logs|paper)$/);
  await expect(page.locator(".shell-header")).toBeVisible();

  const mobile = await page.locator(".set-nav__mobile select").isVisible();
  if (!mobile) {
    // Grouped desktop nav lists every section.
    const nav = page.locator(".set-nav__list");
    await expect(nav.getByRole("link", { name: "Risk limits" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Paper account" })).toBeVisible();
  } else {
    // Phone width: the Section select replaces the list.
    const select = page.locator(".set-nav__mobile select");
    await expect(select).toBeVisible();
    await select.selectOption("exits");
    await expect(page).toHaveURL(/\/settings\/exits/);
    await select.selectOption("risk");
  }

  // Edit on risk, switch to exits and back: the draft survives.
  const risk = page.getByLabel("Risk per trade", { exact: true });
  await risk.fill("0.9");
  if (mobile) {
    await page.locator(".set-nav__mobile select").selectOption("exits");
    await expect(page).toHaveURL(/\/settings\/exits/);
    await page.locator(".set-nav__mobile select").selectOption("risk");
  } else {
    const nav = page.locator(".set-nav__list");
    await nav.getByRole("link", { name: "Exits" }).click();
    await expect(page).toHaveURL(/\/settings\/exits/);
    await nav.getByRole("link", { name: "Risk limits" }).click();
  }
  await expect(page.getByLabel("Risk per trade", { exact: true })).toHaveValue("0.9");
});

test("search and changed-from-default filter", async ({ page }) => {
  await page.goto("/settings/risk");
  await page.getByLabel("Find a setting").fill("kill");
  await expect(page.getByLabel("Drawdown kill")).toBeVisible();
  await expect(page.getByLabel("Risk per trade", { exact: true })).toHaveCount(0);
  await page.getByLabel("Find a setting").fill("");

  // Change one value: it counts as changed-from-default and the filter
  // keeps only changed rows.
  await page.getByLabel("Risk per trade", { exact: true }).fill("0.9");
  await page.getByLabel(/Changed from default/).check();
  await expect(page.getByLabel("Risk per trade", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Min reward to risk")).toHaveCount(0);
});

test("looser change asks for confirmation", async ({ page }) => {
  await page.goto("/settings/risk");
  // Higher kill drawdown is looser.
  const kill = page.getByLabel("Drawdown kill");
  const before = Number(await kill.inputValue());
  await kill.fill(String(before + 1));
  await page.getByLabel("Reason (shown in History)").fill("e2e: looser kill");
  await page.getByRole("button", { name: /Save as v\d+/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(/loosen/i);
  await expect(dialog).toContainText("Drawdown kill");
  await dialog.getByRole("button", { name: "Cancel" }).click();
});

test("history section lists versions", async ({ page }) => {
  await page.goto("/settings/history");
  await expect(page.locator(".shell-header")).toBeVisible();
  await expect(page.locator(".set-table")).toBeVisible();
});

test("screenshots: risk unsaved, save bar, history", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "w1100", "only w1440 and w390 shots");
  const w = testInfo.project.name;

  await page.goto("/settings/risk");
  await expect(page.locator(".shell-header")).toBeVisible();
  await page.waitForLoadState("networkidle");

  // Two unsaved (tighter) changes for the shot.
  await page.getByLabel("Risk per trade", { exact: true }).fill("0.8");
  const daily = page.getByLabel("Daily loss limit");
  await daily.fill(String(Math.max(0.5, Number(await daily.inputValue()) - 0.5)));
  await expect(page.locator(".set-savebar")).toContainText("2 unsaved changes");
  await page.screenshot({
    path: `${SHOT_DIR}/settings-risk-unsaved-${w}.png`,
    fullPage: w === "w1440",
  });

  if (w === "w390") {
    // Phone: open the save bar review.
    await page.locator(".set-savebar__summary").click();
    await page.screenshot({ path: `${SHOT_DIR}/settings-savebar-phone-${w}.png` });
  }

  await page.goto("/settings/history");
  await expect(page.locator(".set-table")).toBeVisible();
  await page.waitForLoadState("networkidle");
  await page.screenshot({
    path: `${SHOT_DIR}/settings-history-${w}.png`,
    fullPage: w === "w1440",
  });
});

test("screenshots: looser confirmation dialog", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "w1440");
  await page.goto("/settings/risk");
  const kill = page.getByLabel("Drawdown kill");
  await kill.fill(String(Number(await kill.inputValue()) + 1));
  await page.getByLabel("Reason (shown in History)").fill("e2e: looser kill");
  await page.getByRole("button", { name: /Save as v\d+/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await page.screenshot({ path: `${SHOT_DIR}/settings-looser-confirm-w1440.png` });
  await dialog.getByRole("button", { name: "Cancel" }).click();
});
