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

test("disconnected pill when the snapshot socket is down", async ({ page }, testInfo) => {
  // Kill the /ws socket as soon as it opens; the pill must show Disconnected,
  // never a stale "Paused".
  await page.routeWebSocket("**/ws", (ws) => {
    ws.close();
  });
  await page.goto("/");
  const pill = page.locator(".topbar .status-pill");
  await expect(pill).toHaveText(/Disconnected/);
  await expect(pill).toHaveAttribute("title", /Last known: Paused/);
  await page.screenshot({
    path: `screenshots/disconnected-${testInfo.project.name}.png`,
  });
});
