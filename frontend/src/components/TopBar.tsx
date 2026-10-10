import type { ReactNode } from "react";
import type { AgentStatus } from "../api/client";
import type { DisplayStatus } from "../lib/status";
import { Button } from "./Button";
import { ErrorBadge } from "./ErrorBadge";
import { SegmentedControl } from "./SegmentedControl";
import { StatusPill } from "./StatusPill";

/** 76 px bar per screen: title + status left, actions right; wraps narrow. */
export function TopBar({
  title,
  status,
  lastKnown,
  errors24h = 0,
  running = false,
  controls = true,
  actions,
  onRunNow,
  onTogglePause,
  onFlatten,
}: {
  title: string;
  status: DisplayStatus;
  lastKnown?: { status: AgentStatus; at: number };
  errors24h?: number;
  running?: boolean;
  /** Show the global controls: Paper/Live, Run now, Pause, Stop & flatten. */
  controls?: boolean;
  actions?: ReactNode;
  onRunNow?: () => void;
  onTogglePause?: () => void;
  onFlatten?: () => void;
}) {
  const noEngine = !onRunNow && !onTogglePause && !onFlatten;
  const controlTitle = noEngine
    ? "Available once the engine lands (build step 3)"
    : undefined;
  return (
    <header className="topbar">
      <div className="topbar__left">
        <h1 className="topbar__title">{title}</h1>
        <StatusPill status={status} lastKnown={lastKnown} />
        <ErrorBadge count={errors24h} href="/activity" />
      </div>
      {controls && (
        <div className="topbar__actions">
          {actions}
          <SegmentedControl
            label="Trading mode"
            value="paper"
            options={[
              { value: "paper", label: "Paper" },
              {
                value: "live",
                label: "Live",
                disabled: true,
                title: "Live trading comes after the paper phase",
              },
            ]}
          />
          <Button
            variant="secondary"
            title={controlTitle}
            disabled={noEngine}
            onClick={onRunNow}
            icon={
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <polyline points="20 6 20 11 15 11" />
                <path d="M20 11a8 8 0 1 0-2.3 5.7" />
              </svg>
            }
          >
            Run now
          </Button>
          <Button
            variant="secondary"
            title={controlTitle}
            disabled={noEngine}
            onClick={onTogglePause}
            icon={
              running ? (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <line x1="8" y1="5" x2="8" y2="19" />
                  <line x1="16" y1="5" x2="16" y2="19" />
                </svg>
              ) : (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <polygon points="6 4 20 12 6 20 6 4" />
                </svg>
              )
            }
          >
            {running ? "Pause" : "Resume"}
          </Button>
          <Button
            variant="danger"
            title={controlTitle}
            disabled={noEngine}
            onClick={onFlatten}
            icon={
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="12" cy="12" r="9" />
                <line x1="12" y1="7" x2="12" y2="13" />
                <line x1="12" y1="17" x2="12" y2="17" />
              </svg>
            }
          >
            Stop &amp; flatten
          </Button>
        </div>
      )}
    </header>
  );
}
