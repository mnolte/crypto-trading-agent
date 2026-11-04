import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Snapshot } from "../api/client";

/** The API hooks module is mocked so the shell renders with a fixed
 * snapshot and no WebSocket. */
const snapshot = {
  status: "running",
  needs: { items: [], health: [] },
  latest: {},
  today_pnl: "0",
  equity: "10000",
} as unknown as Snapshot;

const mutate = vi.fn();
const mutation = { mutate, isPending: false };

vi.mock("../api/hooks", () => ({
  useSnapshotStream: () => ({ connected: true }),
  useSnapshot: () => ({ data: snapshot, dataUpdatedAt: 0, isError: false }),
  useRunNow: () => mutation,
  usePauseAgent: () => mutation,
  useResumeAgent: () => mutation,
  useFlatten: () => mutation,
  useAcknowledgeKill: () => mutation,
  useFlattenPreview: () => ({ data: undefined }),
  useNow: () => Date.now(),
}));

import { AppShell } from "./AppShell";
import { Header } from "./Header";

function needsSnapshot(count: number): Snapshot {
  return {
    ...snapshot,
    needs: {
      items: Array.from({ length: count }, (_, i) => ({
        kind: "errors",
        title: `item ${i}`,
        detail: null,
        action_label: "Review",
        action_target: "/inspector",
        since: null,
      })),
      health: [],
    },
  } as unknown as Snapshot;
}

function renderShell(snap: Snapshot) {
  (snapshot as { needs: object }).needs = snap.needs;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AppShell title="Dashboard" sub="today" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AppShell", () => {
  afterEach(cleanup);
  beforeEach(() => {
    (snapshot as { needs: object }).needs = { items: [], health: [] };
  });

  it("renders the phone top bar and the sidebar; CSS swaps them at 820 px", () => {
    renderShell(needsSnapshot(0));
    expect(document.querySelector(".ta-phonebar")).toBeTruthy();
    expect(document.querySelector(".ta-aside")).toBeTruthy();
    expect(document.querySelector(".shell-header")).toBeTruthy();
  });

  it("shows the Needs you chip only when the count is above 0", () => {
    const { unmount } = renderShell(needsSnapshot(2));
    expect(screen.getAllByText(/Needs you · 2/).length).toBeGreaterThan(0);
    unmount();
    renderShell(needsSnapshot(0));
    expect(screen.queryByText(/Needs you ·/)).toBeNull();
  });

  it("opens the sidebar overlay from the menu button and Escape closes it", () => {
    renderShell(needsSnapshot(0));
    fireEvent.click(screen.getByRole("button", { name: "Open menu" }));
    expect(screen.getByRole("dialog", { name: "Menu" })).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Menu" })).toBeNull();
  });
});

describe("Header", () => {
  const noop = () => {};
  const base = {
    title: "Dashboard",
    sub: "today",
    status: "running" as const,
    onAsk: noop,
    onTogglePause: noop,
    onFlatten: noop,
  };

  it("hides the Needs you chip at 0 items", () => {
    render(
      <MemoryRouter>
        <Header {...base} snapshot={needsSnapshot(0)} />
      </MemoryRouter>,
    );
    expect(document.querySelector(".needs-chip")).toBeNull();
  });

  it("shows the Needs you chip with the count", () => {
    render(
      <MemoryRouter>
        <Header {...base} snapshot={needsSnapshot(3)} />
      </MemoryRouter>,
    );
    expect(document.querySelector(".needs-chip")?.textContent).toContain(
      "Needs you · 3",
    );
  });
});
