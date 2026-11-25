import { expect, test, type Page } from "@playwright/test";
import type { MarketOut, Regime } from "../src/api/client";

function market(pair: string, regime: Regime | null): MarketOut {
  return {
    active: true,
    checked_at: null,
    chg_24h_pct: "0.40",
    daily_candles: 120,
    eligible: true,
    failed_rule: null,
    last_action_action: "HOLD",
    last_action_at: "2026-10-14T10:00:00Z",
    market: pair,
    paused: false,
    price: "100.00",
    regime,
    rules_detail: "Passed",
    rules_state: "passed",
    spread_pct: "0.10",
    status: "active",
    volume_30d_eur: "1000000",
    warming_up: false,
  };
}

const MARKETS: MarketOut[] = [
  market("BTC-EUR", { trend: "up", volatility: "low", age_4h: 14 }),
  market("ETH-EUR", { trend: "up", volatility: "normal", age_4h: 14 }),
  market("SOL-EUR", { trend: "up", volatility: "high", age_4h: 14 }),
  market("XRP-EUR", { trend: "down", volatility: "low", age_4h: 8 }),
  market("ADA-EUR", { trend: "down", volatility: "normal", age_4h: 128 }),
  market("LINK-EUR", { trend: "down", volatility: "high", age_4h: 3 }),
  market("AVAX-EUR", { trend: "sideways", volatility: "low", age_4h: 30 }),
  market("DOGE-EUR", { trend: "sideways", volatility: "normal", age_4h: 3 }),
  market("LTC-EUR", { trend: "sideways", volatility: "high", age_4h: 128 }),
  market("USDC-EUR", null),
];

async function installMarketsFixture(page: Page) {
  await page.route("**/api/markets", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(MARKETS),
    });
  });
}

test.beforeEach(async ({ page }) => {
  await installMarketsFixture(page);
});

test("regime chips in the Markets card stay inside their column", async ({ page }) => {
  await page.goto("/");
  const rows = page.locator(".markets-row:not(.markets-row--head)");
  await expect(rows).toHaveCount(MARKETS.length, { timeout: 10_000 });

  for (let index = 0; index < MARKETS.length; index += 1) {
    const row = rows.nth(index);
    const cell = row.locator(".markets-regime");
    const chip = cell.locator(".regime-chip");
    const rules = row.locator(".markets-rules");
    await expect(chip).toBeVisible();

    const cellBox = await cell.boundingBox();
    const chipBox = await chip.boundingBox();
    const rulesBox = await rules.boundingBox();
    if (!cellBox || !chipBox || !rulesBox) {
      throw new Error(`missing Markets row geometry for ${MARKETS[index].market}`);
    }

    expect(chipBox.width).toBeLessThanOrEqual(cellBox.width + 0.5);
    expect(chipBox.x + chipBox.width).toBeLessThanOrEqual(
      cellBox.x + cellBox.width + 0.5,
    );
    expect(cellBox.x + cellBox.width).toBeLessThanOrEqual(rulesBox.x + 0.5);
  }
});
