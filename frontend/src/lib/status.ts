import type { AgentStatus } from "../api/client";
import { formatTime } from "./format";

/**
 * The pill shows the agent status only while the snapshot WebSocket feeds it.
 * When the socket is down — or before the first snapshot — it shows
 * "Disconnected" instead of falling back to a stale agent state.
 */
export type DisplayStatus = AgentStatus | "disconnected";

export function displayStatus(
  connected: boolean,
  status: AgentStatus | undefined,
): DisplayStatus {
  return connected && status ? status : "disconnected";
}

export function statusLabel(status: DisplayStatus): string {
  if (status === "running") return "Running · paper";
  if (status === "disconnected") return "Disconnected";
  return status.charAt(0).toUpperCase() + status.slice(1);
}

/** "Last known: Running, 14:32" — the tooltip while disconnected. */
export function lastKnownText(status: AgentStatus, at: number): string {
  return `Last known: ${statusLabel(status)}, ${formatTime(new Date(at).toISOString())}`;
}
