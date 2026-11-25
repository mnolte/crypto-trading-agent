import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import type { MarketOut } from "../api/client";
import { RegimeChip } from "../components/RegimeChip";
import { MarketsCard } from "./Dashboard";

const sidewaysNewMarket: MarketOut = {
  active: true,
  checked_at: null,
  chg_24h_pct: "0.4",
  daily_candles: 120,
  eligible: true,
  failed_rule: null,
  last_action_action: null,
  last_action_at: null,
  market: "SID-EUR",
  paused: false,
  price: "1.25",
  regime: { trend: "sideways", volatility: "normal", age_4h: 3 },
  rules_detail: "Passed",
  rules_state: "passed",
  spread_pct: "0.1",
  status: "active",
  volume_30d_eur: "1000000",
  warming_up: false,
};

describe("MarketsCard", () => {
  afterEach(cleanup);

  it("renders a compact regime chip with the age and new state in the title", () => {
    render(
      <MemoryRouter>
        <MarketsCard
          markets={[sidewaysNewMarket]}
          heldPairs={[]}
          selected={null}
          paused={new Map()}
          onPick={() => {}}
        />
      </MemoryRouter>,
    );

    const row = screen.getByRole("button", { name: "SID-EUR, chart it" });
    const chip = row.querySelector(".regime-chip");
    expect(chip?.textContent).toBe("sideways · normal vol");
    const title = chip?.getAttribute("title") ?? "";
    expect(title).toContain("3 closed 4h bars");
    expect(title).toContain("new: the regime changed less than 6 bars ago");
  });

  it("keeps the age in the full regime chip used outside Markets", () => {
    render(
      <RegimeChip
        regime={{ trend: "sideways", volatility: "normal", age_4h: 3 }}
      />,
    );

    expect(screen.getByText("sideways · normal vol · 3 bars · new")).toBeTruthy();
  });
});
