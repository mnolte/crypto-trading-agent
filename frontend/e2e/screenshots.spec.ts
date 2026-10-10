import { expect, test } from "@playwright/test";

const PAGES = [
  { path: "/", name: "dashboard" },
  { path: "/settings", name: "settings" },
  { path: "/dev/components", name: "components" },
];

for (const { path, name } of PAGES) {
  test(`screenshot ${name}`, async ({ page }, testInfo) => {
    await page.goto(path);
    await expect(page.locator(".topbar")).toBeVisible();
    await page.screenshot({
      path: `screenshots/${name}-${testInfo.project.name}.png`,
      fullPage: name === "components",
    });
  });
}
