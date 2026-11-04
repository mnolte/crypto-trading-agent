import { useEffect, useSyncExternalStore, type ReactNode } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { formatTime } from "./format";

/** Card states (design/DESIGN.md → Interaction rules): every card that
 * fetches shows loading (skeleton, aria-busy), empty (only after a
 * successful load, with the reason) or error (what failed and when,
 * Retry, last data kept and marked stale). When two or more cards fail
 * the page shows the backend-unreachable banner. */

const failing = new Set<string>();
const listeners = new Set<() => void>();
let failingSince: number | null = null;

function emit() {
  for (const l of listeners) l();
}

export function reportCardState(name: string, failed: boolean): void {
  const had = failing.has(name);
  if (failed === had) return;
  if (failed) failing.add(name);
  else failing.delete(name);
  failingSince = failing.size > 0 ? (failingSince ?? Date.now()) : null;
  emit();
}

export function useFailingCards(): { count: number; since: number | null } {
  const count = useSyncExternalStore(listen, () => failing.size);
  const since = useSyncExternalStore(listen, () => failingSince);
  return { count, since };
}

function listen(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** Wrap a card body in its fetch state. `query` is any TanStack query;
 * `name` identifies the card for the failure registry; `empty` renders
 * only after a successful load; `stale` children keep last data visible
 * but marked stale while an error shows above them. */
export function CardFetch({
  query,
  name,
  empty,
  emptyWhen = false,
  children,
}: {
  query: UseQueryResult<unknown, Error>;
  name: string;
  empty?: ReactNode;
  emptyWhen?: boolean;
  children: ReactNode;
}) {
  const { isPending, isError, error, dataUpdatedAt, refetch, isFetching } = query;
  const failed = isError && !isFetching;

  useEffect(() => {
    reportCardState(name, failed);
    return () => reportCardState(name, false);
  }, [name, failed]);

  if (isPending && !dataUpdatedAt) {
    return (
      <div className="card-state" aria-busy="true">
        <div className="ta-skel" style={{ height: 12, width: "70%", borderRadius: 4 }} />
        <div className="ta-skel" style={{ height: 12, width: "90%", borderRadius: 4 }} />
        <div className="ta-skel" style={{ height: 12, width: "55%", borderRadius: 4 }} />
        <span className="card-state__hint">Loading…</span>
      </div>
    );
  }
  if (failed) {
    return (
      <div className="card-state" data-stale={dataUpdatedAt ? "true" : undefined}>
        <div className="card-state__error">
          <WarnIcon />
          <span>
            Couldn’t load {name} at {formatTime(new Date().toISOString())} (
            {error instanceof Error ? error.message : "request failed"}
            {error instanceof ApiError && error.errorId
              ? ` · ${error.errorId}`
              : ""}
            ).
            {dataUpdatedAt
              ? ` Showing data from ${formatTime(new Date(dataUpdatedAt).toISOString())}.`
              : ""}
          </span>
        </div>
        <button
          type="button"
          className="card-state__retry"
          onClick={() => void refetch()}
        >
          Retry
        </button>
        {dataUpdatedAt > 0 && (
          <div className="card-state__stale">{children}</div>
        )}
      </div>
    );
  }
  if (emptyWhen) {
    return <div className="card-state">{empty}</div>;
  }
  return <>{children}</>;
}

function WarnIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ flex: "0 0 16px" }}
    >
      <path d="M12 3l10 18H2z" />
      <line x1="12" y1="10" x2="12" y2="14" />
      <line x1="12" y1="18" x2="12" y2="18" />
    </svg>
  );
}
