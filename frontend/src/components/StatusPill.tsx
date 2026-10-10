import type { AgentStatus } from "../api/client";
import { lastKnownText, statusLabel, type DisplayStatus } from "../lib/status";

export function StatusPill({
  status,
  lastKnown,
}: {
  status: DisplayStatus;
  /** While disconnected, the stale agent state and when it last arrived. */
  lastKnown?: { status: AgentStatus; at: number };
}) {
  const title =
    status === "disconnected" && lastKnown
      ? lastKnownText(lastKnown.status, lastKnown.at)
      : undefined;
  return (
    <span className={`status-pill status-pill--${status}`} title={title}>
      <span className="status-pill__dot" aria-hidden="true" />
      <span>{statusLabel(status)}</span>
    </span>
  );
}
