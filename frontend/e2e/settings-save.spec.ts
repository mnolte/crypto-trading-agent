import { test, expect } from "@playwright/test";

// Item 025: the sectioned Settings screen saves a draft as a numbered
// version with a required reason.
test("settings save persists a sectioned change", async ({ page }) => {
  let putBody: {
    settings?: { risk?: { risk_per_trade_pct?: unknown } };
    reason?: string;
    confirm_looser?: boolean;
  } | null = null;
  page.on("request", (req) => {
    if (req.url().includes("/api/settings") && req.method() === "PUT") {
      putBody = req.postDataJSON() as typeof putBody;
    }
  });

  await page.goto("/settings/risk");
  await expect(page.locator(".shell-header")).toBeVisible();

  // One row per setting: "Risk per trade" is a number input with its unit.
  const risk = page.getByLabel("Risk per trade", { exact: true });
  await expect(risk).toBeVisible();
  const before = Number(await risk.inputValue());
  // Always tighter (lower risk per trade) so no looser-confirm is needed.
  const next = Math.max(0.1, before - 0.1).toFixed(1);
  await risk.fill(next);

  // The save bar appears with the unsaved change; the reason is required.
  const save = page.getByRole("button", { name: /Save as v\d+/ });
  await expect(save).toBeVisible();
  await expect(save).toBeDisabled();
  await page.getByLabel("Reason (shown in History)").fill("e2e: tighter risk");
  await expect(save).toBeEnabled();
  await save.click();

  await page.waitForResponse(
    (r) => r.url().includes("/api/settings") && r.request().method() === "PUT",
  );
  const body = putBody as {
    settings?: { risk?: { risk_per_trade_pct?: unknown } };
    reason?: string;
    confirm_looser?: boolean;
  } | null;
  expect(Number(body?.settings?.risk?.risk_per_trade_pct)).toBe(Number(next));
  expect(body?.reason).toBe("e2e: tighter risk");
  expect(body?.confirm_looser).toBe(false);

  // The change survives a reload.
  await page.reload();
  await expect(page.getByLabel("Risk per trade", { exact: true })).toHaveValue(
    next,
  );
});
