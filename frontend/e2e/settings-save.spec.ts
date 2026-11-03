import { test, expect } from "@playwright/test";

// Regression: the Settings top bar must render Discard/Save even though the
// page hides the trading controls — otherwise no setting can be persisted.
test("settings save button persists changes across reload", async ({ page }) => {
  let putBody: { notifications?: { browser_enabled?: boolean } } | null = null;
  page.on("request", (req) => {
    if (req.url().includes("/api/settings") && req.method() === "PUT") {
      putBody = req.postDataJSON() as typeof putBody;
    }
  });
  await page.goto("/settings");

  // Wait until the draft has loaded from GET /api/settings (an event checkbox
  // that defaults on proves it) before reading the toggle state.
  await expect(page.getByLabel("kill_switch browser")).toBeChecked();
  const toggle = page.getByLabel("Browser notifications");
  const wasChecked = await toggle.isChecked();
  await toggle.click();

  const save = page.getByRole("button", { name: /Save · applies next cycle/ });
  await expect(save).toBeEnabled();
  await save.click();
  await page.waitForResponse(
    (r) => r.url().includes("/api/settings") && r.request().method() === "PUT",
  );
  const body = putBody as { notifications?: { browser_enabled?: boolean } } | null;
  expect(body?.notifications?.browser_enabled).toBe(!wasChecked);

  await page.reload();
  await expect(page.getByLabel("kill_switch browser")).toBeChecked();
  await expect(toggle).toBeChecked({ checked: !wasChecked });
});
