import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import type {
  Settings as SettingsType,
  SettingsMetaItem,
  SettingsMetaOut,
  SettingsPreviewOut,
  SettingsSaveIn,
} from "../api/client";
import { ApiError } from "../api/client";

/** The API hooks module is mocked: the page renders with a fixed settings
 * document, meta and preview results, and the save/preview mutations are
 * spies the tests drive by hand. */
const h = vi.hoisted(() => ({
  saveMutate: vi.fn(),
  previewMutate: vi.fn(),
  previewResult: {
    changes: [],
    equivalents: {},
    errors: {},
    summary: {
      daily_stop_eur: "83.30",
      day_start_equity: "10326.60",
      equity: "10412.80",
      kill_level_eur: "9647.30",
      loss_at_stop_eur: "52.06",
      next_position_pct: "0.1",
      peak_equity: "10486.20",
      positions_at_full_risk: 3,
      throttled_loss_eur: "40.00",
    },
  } as unknown as SettingsPreviewOut,
}));

const DOC = {
  active_markets: ["BTC-EUR"],
  execution_mode: "auto",
  risk: { risk_per_trade_pct: 1, min_rr: 2 },
  ui: { stop_near_atr: 1 },
  learning: { plain_language: false, quiet_line: true },
} as unknown as SettingsType;

function metaItem(section: string, dflt: unknown, unit: string | null = null): SettingsMetaItem {
  return { section, unit, default: dflt, fixed: false };
}

const META: SettingsMetaOut = {
  version: 7,
  items: {
    "risk.risk_per_trade_pct": metaItem("risk", 1, "%"),
    "risk.min_rr": metaItem("risk", 2, "×"),
    "ui.stop_near_atr": metaItem("display", 1, "ATR"),
    "learning.plain_language": metaItem("display", false),
    "learning.quiet_line": metaItem("display", true),
    "execution_mode": metaItem("execution", "auto"),
    "active_markets": metaItem("markets", ["BTC-EUR"]),
  },
};

vi.mock("../api/hooks", () => ({
  useSettings: () => ({ data: DOC }),
  useSettingsMeta: () => ({ data: META }),
  useUniverse: () => ({ data: undefined }),
  usePrompts: () => ({ data: [] }),
  useSaveSettings: () => ({ mutate: h.saveMutate, isPending: false }),
  useSettingsPreview: () => ({ mutate: h.previewMutate }),
}));

vi.mock("../components/AppShell", async () => {
  const { createElement } = await import("react");
  return {
    AppShell: ({ children }: { children?: ReactNode }) =>
      createElement("div", null, children),
  };
});

import { Settings } from "./Settings";

function renderSettings(entry = "/settings/risk") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/settings/:section?" element={<Settings />} />
        <Route path="*" element={<div>elsewhere</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function nav() {
  return within(document.querySelector(".set-nav") as HTMLElement);
}

async function editRiskPerTrade(value: string) {
  const input = await screen.findByLabelText("Risk per trade");
  fireEvent.change(input, { target: { value } });
  // The preview runs 300 ms after typing stops.
  await waitFor(() => expect(h.previewMutate).toHaveBeenCalled());
}

async function enterReason(text = "tuning for the week") {
  const input = await screen.findByLabelText("Reason (shown in History)");
  fireEvent.change(input, { target: { value: text } });
}

describe("Settings page", () => {
  beforeEach(() => {
    h.previewResult = {
      changes: [],
      equivalents: {},
      errors: {},
      summary: {
        daily_stop_eur: "83.30",
        day_start_equity: "10326.60",
        equity: "10412.80",
        kill_level_eur: "9647.30",
        loss_at_stop_eur: "52.06",
        next_position_pct: "0.1",
        peak_equity: "10486.20",
        positions_at_full_risk: 3,
        throttled_loss_eur: "40.00",
      },
    } as unknown as SettingsPreviewOut;
    h.previewMutate.mockImplementation((_doc: unknown, opts?: { onSuccess?: (r: SettingsPreviewOut) => void }) =>
      opts?.onSuccess?.(h.previewResult),
    );
    h.saveMutate.mockImplementation(
      (body: SettingsSaveIn, opts?: { onSuccess?: (r: { settings: SettingsType; version: number }) => void }) =>
        opts?.onSuccess?.({ settings: body.settings, version: 8 }),
    );
    window.localStorage.clear();
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    document.body.innerHTML = "";
  });

  it("keeps the draft when switching sections", async () => {
    renderSettings("/settings/risk");
    await editRiskPerTrade("0.8");
    expect(screen.getByText(/1 unsaved change/)).toBeTruthy();

    fireEvent.click(nav().getByRole("link", { name: /Exits/ }));
    expect(await screen.findByRole("heading", { name: "Exits" })).toBeTruthy();

    fireEvent.click(nav().getByRole("link", { name: /Risk limits/ }));
    const input = await screen.findByLabelText("Risk per trade");
    expect((input as HTMLInputElement).value).toBe("0.8");
    expect(screen.getByText(/1 unsaved change/)).toBeTruthy();
  });

  it("Find and Changed from default cover rows in several sections", async () => {
    renderSettings("/settings/risk");
    await editRiskPerTrade("0.8");

    fireEvent.click(nav().getByRole("link", { name: /Display/ }));
    const toggle = await screen.findByLabelText("Plain-language mode");
    fireEvent.click(toggle);
    await waitFor(() => expect(h.previewMutate).toHaveBeenCalledTimes(2));

    // Both sections carry a "changed from default" count in the nav.
    const riskLink = nav().getByRole("link", { name: /Risk limits/ });
    const displayLink = nav().getByRole("link", { name: /Display/ });
    expect(within(riskLink).getByText("1")).toBeTruthy();
    expect(within(displayLink).getByText("1")).toBeTruthy();

    // Changed from default keeps only the non-default row on this section.
    fireEvent.click(nav().getByRole("link", { name: /Risk limits/ }));
    const filter = await screen.findByLabelText(/Changed from default/);
    fireEvent.click(filter);
    expect(await screen.findByLabelText("Risk per trade")).toBeTruthy();
    expect(screen.queryByLabelText("Min reward to risk")).toBeNull();

    // Find lists matching rows again, changed or not.
    fireEvent.click(filter);
    const find = screen.getByLabelText("Find a setting");
    fireEvent.change(find, { target: { value: "reward" } });
    expect(await screen.findByLabelText("Min reward to risk")).toBeTruthy();
    expect(screen.queryByLabelText("Risk per trade")).toBeNull();
  });

  it("disables Save while the preview reports an error", async () => {
    h.previewResult = {
      changes: [],
      equivalents: {},
      errors: { "risk.risk_per_trade_pct": "must be 0.1 to 2" },
      summary: {
        daily_stop_eur: "83.30",
        day_start_equity: "10326.60",
        equity: "10412.80",
        kill_level_eur: "9647.30",
        loss_at_stop_eur: "52.06",
        next_position_pct: "0.1",
        peak_equity: "10486.20",
        positions_at_full_risk: 3,
        throttled_loss_eur: "40.00",
      },
    } as unknown as SettingsPreviewOut;
    renderSettings("/settings/risk");
    await editRiskPerTrade("5");
    await enterReason();

    const save = await screen.findByRole("button", { name: /Save as v8/ });
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(true));
    expect(await screen.findByText("must be 0.1 to 2")).toBeTruthy();
  });

  it("shows the confirm dialog on 409 and resends with confirm_looser", async () => {
    const calls: SettingsSaveIn[] = [];
    h.saveMutate.mockImplementation(
      (body: SettingsSaveIn, opts?: {
        onSuccess?: (r: { settings: SettingsType; version: number }) => void;
        onError?: (e: unknown) => void;
      }) => {
        calls.push(body);
        if (calls.length === 1) {
          opts?.onError?.(
            new ApiError({
              status: 409,
              code: "looser_needs_confirm",
              message: "looser",
              changes: [
                {
                  key: "risk.risk_per_trade_pct",
                  old: 1,
                  new: 0.8,
                  direction: "looser",
                },
              ],
            }),
          );
        } else {
          opts?.onSuccess?.({ settings: body.settings, version: 8 });
        }
      },
    );
    renderSettings("/settings/risk");
    await editRiskPerTrade("0.8");
    await enterReason();

    fireEvent.click(await screen.findByRole("button", { name: /Save as v8/ }));
    expect(calls).toHaveLength(1);
    expect(calls[0].confirm_looser).toBe(false);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/loosens a limit/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Save anyway" }));

    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].confirm_looser).toBe(true);
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).toBeNull(),
    );
  });

  it("asks before leaving with an unsaved draft", async () => {
    renderSettings("/settings/risk");
    await editRiskPerTrade("0.8");

    const beforeUnload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(beforeUnload);
    expect(beforeUnload.defaultPrevented).toBe(true);

    const confirmMock = vi.fn(() => false);
    vi.stubGlobal("confirm", confirmMock);
    const anchor = document.createElement("a");
    anchor.href = "/dashboard";
    anchor.textContent = "leave";
    document.body.appendChild(anchor);

    const click = new MouseEvent("click", { bubbles: true, cancelable: true });
    anchor.dispatchEvent(click);
    expect(confirmMock).toHaveBeenCalled();
    expect(click.defaultPrevented).toBe(true);

    confirmMock.mockReturnValue(true);
    const click2 = new MouseEvent("click", { bubbles: true, cancelable: true });
    anchor.dispatchEvent(click2);
    expect(click2.defaultPrevented).toBe(false);
    vi.unstubAllGlobals();
  });
});
