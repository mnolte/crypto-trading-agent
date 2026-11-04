import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { UseQueryResult } from "@tanstack/react-query";
import { CardFetch } from "./cardState";

afterEach(cleanup);

function fakeQuery(partial: Partial<UseQueryResult>): UseQueryResult {
  return {
    data: undefined,
    isPending: false,
    isError: false,
    isFetching: false,
    dataUpdatedAt: 0,
    error: null,
    refetch: vi.fn(),
    ...partial,
  } as unknown as UseQueryResult;
}

describe("CardFetch", () => {
  it("shows a skeleton with aria-busy while loading, never the empty text", () => {
    const { container } = render(
      <CardFetch
        query={fakeQuery({ isPending: true })}
        name="positions"
        empty={<span>No open positions.</span>}
      >
        <span>rows</span>
      </CardFetch>,
    );
    expect(container.querySelector("[aria-busy='true']")).toBeTruthy();
    expect(container.querySelector(".ta-skel")).toBeTruthy();
    expect(screen.getByText("Loading…")).toBeTruthy();
    expect(screen.queryByText("No open positions.")).toBeNull();
    expect(screen.queryByText("rows")).toBeNull();
  });

  it("shows the empty state only after a successful load", () => {
    render(
      <CardFetch
        query={fakeQuery({ dataUpdatedAt: 1000 })}
        name="positions"
        empty={<span>No open positions.</span>}
        emptyWhen
      >
        <span>rows</span>
      </CardFetch>,
    );
    expect(screen.getByText("No open positions.")).toBeTruthy();
    expect(screen.queryByText("rows")).toBeNull();
  });

  it("shows the error with a Retry and keeps last data marked stale", () => {
    const refetch = vi.fn();
    render(
      <CardFetch
        query={fakeQuery({
          isError: true,
          error: new Error("request timed out"),
          dataUpdatedAt: 1000,
          refetch,
        })}
        name="positions"
      >
        <span>rows</span>
      </CardFetch>,
    );
    expect(screen.getByText(/load positions/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalled();
    // Last data stays visible, marked stale.
    expect(screen.getByText("rows")).toBeTruthy();
    expect(document.querySelector("[data-stale='true']")).toBeTruthy();
  });

  it("renders children on a successful load", () => {
    render(
      <CardFetch query={fakeQuery({ dataUpdatedAt: 1 })} name="positions">
        <span>rows</span>
      </CardFetch>,
    );
    expect(screen.getByText("rows")).toBeTruthy();
  });
});
