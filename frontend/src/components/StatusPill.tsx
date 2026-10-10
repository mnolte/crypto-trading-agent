import type { AgentStatus } from "../api/client";

const LABELS: Record<AgentStatus, string> = {
  running: "Running · paper",
  paused: "Paused",
  killed: "Killed",
  error: "Error",
};

export function StatusPill({ status }: { status: AgentStatus }) {
  return (
    <span className={`status-pill status-pill--${status}`}>
      <span className="status-pill__dot" aria-hidden="true" />
      <span>{LABELS[status]}</span>
    </span>
  );
}
