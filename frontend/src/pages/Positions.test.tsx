import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PositionsOut } from "../api/client";

/** The page is tested with the API layer mocked: fixed positions/fills
 * payloads, a stub shell and a stub decision drawer. */

const livePositions: PositionsOut = {
  account: "live",
  shadows: [
    {
      id: "sh-1",
      name: "Shadow A",
      model: "gpt-5",
      prompt_version: "decision.v1",
      status: "running",
      started_at: "2026-10-01T08:00:00Z",
      entry_orders: "limit",
    },
  ],
  totals: {
    count: 0,
    unrealised: "0",
    stop_hit_total: "0",
    open_risk_pct: 0,
    open_risk_cap_pct: 2,
    exposure: "0",
    exposure_pct: 0,
    cash: "10000",
    fees_today: "0",
    fees_total: "0",
    closest_pair: null,
    closest_atr: null,
  },
  positions: [],
  orders: [],
  today: { items: [], entries: 0, exits: 0, realised: "0", fees: "0" },
  paper_account: {
    id: 1,
    started_at: "2026-10-01T08:00:00Z",
    start_balance: "10000",
    cash: "10000",
    coins_value: "0",
    equity: "10000",
    realised_pnl: "0",
    closed_trades: 0,
    fees_total: "0",
    slippage_total: "0",
  },
} as unknown as PositionsOut;

const shadowPositions: PositionsOut = {
  ...livePositions,
  account: "sh-1",
  today: null,
  paper_account: null,
} as unknown as PositionsOut;

const fillsPage = {
  items: [
    {
      id: 7,
      at: "2026-10-14T10:00:00Z",
      pair: "BTC-EUR",
      side: "sell",
      qty: "0.5",
      reference_price: "110",
      fill_price: "109.94",
      order_type: "market",
      fee_eur: "0.14",
      fee_kind: "taker",
      slippage_eur: "0.03",
      reason: "stop",
      decision_id: "d-9",
      decision_action: "SELL",
      decision_at: "2026-10-13T09:00:00Z",
      trade_id: 3,
      paper_order_id: null,
      source: "live",
    },
  ],
  next_cursor: null,
  total: 1,
};

const positionsCalls: string[] = [];
const fillsCalls: unknown[] = [];
const drawerCalls: (string | null)[] = [];

vi.mock("../api/hooks", () => ({
  useSnapshot: () => ({ data: undefined, isError: false }),
  useSettings: () => ({
    data: {
      paper: {
        entry_orders: "market",
        taker_fee_pct: "0.25",
        maker_fee_pct: "0.15",
        slippage_pct: "0.05",
      },
      risk: { trailing_stop_enabled: true },
      ui: { stop_near_atr: 1.5 },
    },
  }),
  useMarkets: () => ({ data: [] }),
  useNow: () => Date.parse("2026-10-14T12:00:00Z"),
  usePositions: (account: string) => {
    positionsCalls.push(account);
    const data = account === "live" ? livePositions : shadowPositions;
    return {
      data,
      isPending: false,
      isError: false,
      isFetching: false,
      isSuccess: true,
      dataUpdatedAt: 1,
      refetch: vi.fn(),
    };
  },
  useFills: (filters: unknown) => {
    fillsCalls.push(filters);
    return {
      data: { pages: [fillsPage] },
      isPending: false,
      isError: false,
      isFetching: false,
      isSuccess: true,
      dataUpdatedAt: 1,
      refetch: vi.fn(),
      hasNextPage: false,
      fetchNextPage: vi.fn(),
    };
  },
}));

vi.mock("../components/AppShell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

vi.mock("../components/DecisionDrawer", () => ({
  DecisionDrawer: ({ id }: { id: string }) => {
    drawerCalls.push(id);
    return <div data-testid="decision-drawer">{id}</div>;
  },
}));

import { Positions } from "./Positions";

function renderPage(url = "/positions") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Positions />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  positionsCalls.length = 0;
  fillsCalls.length = 0;
  drawerCalls.length = 0;
});

describe("Positions", () => {
  it("shows the live empty-positions text", () => {
    renderPage();
    expect(
      screen.getByText(/No open positions\. The next cycle is at/),
    ).toBeTruthy();
    expect(
      screen.getByText(/Live entries are market orders/),
    ).toBeTruthy();
  });

  it("switches to a shadow by name and shows its empty text", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Shadow A" }));
    expect(positionsCalls).toContain("sh-1");
    expect(
      screen.getByText("Shadow Shadow A holds nothing right now."),
    ).toBeTruthy();
    expect(screen.getByText(/Shadow A · gpt-5 · decision\.v1/)).toBeTruthy();
  });

  it("keeps order-history filters in the URL and sends them to the API", () => {
    renderPage("/positions?pair=BTC-EUR&reason=stop");
    expect(fillsCalls.at(-1)).toMatchObject({ pair: "BTC-EUR", reason: "stop" });
    const reasonSelect = screen.getByLabelText("Reason") as HTMLSelectElement;
    expect(reasonSelect.value).toBe("stop");
    const exportLink = screen.getByRole("link", { name: "Export CSV" });
    expect(exportLink.getAttribute("href")).toContain("pair=BTC-EUR");
    expect(exportLink.getAttribute("href")).toContain("reason=stop");
  });

  it("opens the decision drawer from a fill's trade link", () => {
    renderPage();
    // The row renders a desktop and a phone variant; either opens the drawer.
    fireEvent.click(screen.getAllByRole("button", { name: /trade #3/ })[0]);
    expect(screen.getByTestId("decision-drawer").textContent).toBe("d-9");
  });
});
