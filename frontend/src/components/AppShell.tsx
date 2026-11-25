import { useEffect, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import {
  useAcknowledgeKill,
  useFlatten,
  useFlattenPreview,
  usePauseAgent,
  useResumeAgent,
  useSnapshot,
  useSnapshotStream,
} from "../api/hooks";
import {
  formatEur,
  formatPct,
  formatSignedEur,
  formatDateTime,
  formatTime,
} from "../lib/format";
import { useFailingCards } from "../lib/cardState";
import { useFocusTrap } from "../lib/focusTrap";
import { displayStatus } from "../lib/status";
import { ToastStack } from "../lib/toasts";
import { AskPanel } from "./AskPanel";
import { Banner } from "./Banner";
import { ErrorBoundary } from "./ErrorBoundary";
import { Button } from "./Button";
import { Dialog } from "./Dialog";
import { Header, PhoneBar } from "./Header";
import { Sidebar } from "./Sidebar";
import { SidePanel } from "./SlidePanel";

/** The shell (design/DESIGN.md → The shell): sidebar + header on every screen,
 * the phone top bar and sidebar overlay below 820 px, page banners, dialogs
 * and the toast stack. Pages pass their title and sub line; `controls=false`
 * disables Pause and Stop & flatten (Ask always works). */
export function AppShell({
  title,
  sub,
  controls = true,
  children,
}: {
  title: string;
  sub?: ReactNode;
  controls?: boolean;
  children?: ReactNode;
}) {
  const { connected } = useSnapshotStream();
  const snapshotQuery = useSnapshot();
  const { data: snapshot, dataUpdatedAt } = snapshotQuery;
  const status = displayStatus(connected, snapshot?.status);
  const lastKnown = snapshot ? { status: snapshot.status, at: dataUpdatedAt } : undefined;
  const [askOpen, setAskOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [flattenOpen, setFlattenOpen] = useState(false);
  const [killOpen, setKillOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const location = useLocation();
  const queryClient = useQueryClient();
  const failing = useFailingCards();

  const pause = usePauseAgent();
  const resume = useResumeAgent();
  const flatten = useFlatten();
  const acknowledge = useAcknowledgeKill();
  const { data: preview } = useFlattenPreview(flattenOpen);

  const killed = snapshot?.status === "killed" && snapshot.kill != null;
  const needs = snapshot?.needs?.items.length ?? 0;

  // Browser tab: agent state first (design → Interaction rules → Page title).
  useEffect(() => {
    const icon =
      status === "running"
        ? "● Running"
        : status === "paused"
          ? "⏸ Paused"
          : status === "killed"
            ? "⛔ Killed"
            : status === "error"
              ? "⚠ Error"
              : null;
    document.title = icon ? `${icon} · Trading Agent` : "Trading Agent";
  }, [status]);

  // Ctrl/⌘+J opens the Ask panel from anywhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        setAskOpen(true);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // The phone menu closes on navigation and locks body scroll while open.
  const locationKey = `${location.pathname}${location.search}`;
  const [prevLocationKey, setPrevLocationKey] = useState(locationKey);
  if (locationKey !== prevLocationKey) {
    setPrevLocationKey(locationKey);
    setMenuOpen(false);
  }
  useEffect(() => {
    if (!menuOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [menuOpen]);

  const busy = pause.isPending || resume.isPending || flatten.isPending;
  const controlsDisabled = !controls
    ? "Agent controls are on the Dashboard"
    : killed
      ? "Acknowledge the kill first"
      : busy
        ? "Working…"
        : undefined;

  const onError = (error: unknown) =>
    setActionError(error instanceof Error ? error.message : String(error));

  // Backend unreachable: two or more cards fail or the snapshot query itself.
  const unreachable = failing.count >= 2 || snapshotQuery.isError;
  const since =
    failing.since ??
    (unreachable ? snapshotQuery.errorUpdatedAt || null : null);
  useEffect(() => {
    if (!unreachable) return;
    const id = window.setInterval(() => {
      void queryClient.invalidateQueries();
    }, 15_000);
    return () => window.clearInterval(id);
  }, [unreachable, queryClient]);

  return (
    <div className="shell">
      <PhoneBar
        title={title}
        needs={needs}
        menuOpen={menuOpen}
        onAsk={() => setAskOpen(true)}
        onMenu={() => setMenuOpen(true)}
      />
      <div className="ta-aside shell__aside">
        <Sidebar status={status} lastKnown={lastKnown} />
      </div>
      <div className="shell__main">
        <Header
          title={title}
          sub={sub}
          status={status}
          lastKnown={lastKnown}
          snapshot={snapshot}
          disabledTitle={controlsDisabled}
          onAsk={() => setAskOpen(true)}
          onTogglePause={() =>
            status === "running"
              ? pause.mutate(undefined, { onError })
              : resume.mutate(undefined, { onError })
          }
          onFlatten={() => setFlattenOpen(true)}
        />
        <ToastStack />
        <main className="page">
          <ErrorBoundary>
          {unreachable && (
            <div role="status" className="unreachable">
              <WarnIcon />
              <span className="unreachable__text">
                <b>
                  Can’t reach the Trading Agent backend
                  {since ? ` since ${formatTime(new Date(since).toISOString())}` : ""}.
                </b>{" "}
                Showing data from{" "}
                {formatTime(new Date(dataUpdatedAt || since || 0).toISOString())},
                marked stale. Retrying every 15 seconds. If only this browser
                lost its connection, the agent keeps running and watching stops.
              </span>
              <button
                type="button"
                className="unreachable__retry"
                onClick={() => void queryClient.invalidateQueries()}
              >
                Retry now
              </button>
            </div>
          )}
          {killed && snapshot.kill && (
            <Banner
              variant="danger"
              actions={
                <Button variant="danger-solid" onClick={() => setKillOpen(true)}>
                  Review and acknowledge
                </Button>
              }
            >
              Kill switch fired at {formatDateTime(snapshot.kill.at)}: drawdown −
              {formatPct(snapshot.kill.drawdown_pct, 1)} from peak{" "}
              {formatEur(snapshot.kill.peak, 0)}.{" "}
              {snapshot.kill.closed.length > 0
                ? `All ${snapshot.kill.closed.length} positions were closed at market. `
                : ""}
              No new trades until you acknowledge.
            </Banner>
          )}
          {actionError && (
            <Banner
              variant="warning"
              actions={
                <Button variant="secondary" onClick={() => setActionError(null)}>
                  Dismiss
                </Button>
              }
            >
              {actionError}
            </Banner>
          )}
          {children}
          </ErrorBoundary>
        </main>
      </div>

      {menuOpen && (
        <MenuOverlay status={status} lastKnown={lastKnown} onClose={() => setMenuOpen(false)} />
      )}

      {askOpen && (
        <SidePanel
          label="Ask the agent"
          title="Ask the agent"
          onClose={() => setAskOpen(false)}
        >
          <AskPanel />
        </SidePanel>
      )}

      {flattenOpen && (
        <Dialog
          title="Close all positions and pause?"
          onClose={() => setFlattenOpen(false)}
          actions={[
            { label: "Cancel", onClick: () => setFlattenOpen(false) },
            {
              label: "Close all and pause",
              variant: "danger-outline",
              onClick: () =>
                flatten.mutate(undefined, {
                  onError,
                  onSettled: () => setFlattenOpen(false),
                }),
            },
          ]}
        >
          {preview ? (
            preview.count > 0 ? (
              <p>
                {preview.count}{" "}
                {preview.count === 1 ? "position" : "positions"} will be closed
                at market: {preview.pairs.join(", ")}. Estimated fees{" "}
                {formatEur(preview.est_fees_eur)}. Unrealised P&amp;L right now:{" "}
                {formatSignedEur(preview.unrealised_eur)}.
              </p>
            ) : (
              <p>
                No positions are open. The agent pauses; press Resume when
                ready.
              </p>
            )
          ) : (
            <p>Checking open positions…</p>
          )}
        </Dialog>
      )}

      {killOpen && snapshot?.kill && (
        <KillDialog
          kill={snapshot.kill}
          equity={snapshot.equity}
          pending={acknowledge.isPending}
          onCancel={() => setKillOpen(false)}
          onAcknowledge={() =>
            acknowledge.mutate(undefined, {
              onError,
              onSettled: () => setKillOpen(false),
            })
          }
        />
      )}
    </div>
  );
}

/** The sidebar as an overlay menu at phone width: dim backdrop, focus trap,
 * Escape and backdrop click close it (design → Dialogs and drawers). */
function MenuOverlay({
  status,
  lastKnown,
  onClose,
}: {
  status: Parameters<typeof Sidebar>[0]["status"];
  lastKnown?: Parameters<typeof Sidebar>[0]["lastKnown"];
  onClose: () => void;
}) {
  const ref = useFocusTrap<HTMLDivElement>(true, onClose);
  return (
    <div className="menu-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label="Menu"
        className="menu-overlay__panel"
        tabIndex={-1}
      >
        <Sidebar status={status} lastKnown={lastKnown} forceExpanded />
      </div>
    </div>
  );
}

function WarnIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 16px" }}>
      <path d="M12 3l10 18H2z" />
      <line x1="12" y1="10" x2="12" y2="14" />
      <line x1="12" y1="18" x2="12" y2="18" />
    </svg>
  );
}

function KillDialog({
  kill,
  equity,
  pending,
  onCancel,
  onAcknowledge,
}: {
  kill: { at: string; peak: string; equity: string; drawdown_pct: string; closed: string[] };
  equity: string;
  pending: boolean;
  onCancel: () => void;
  onAcknowledge: () => void;
}) {
  const [checked, setChecked] = useState(false);
  return (
    <Dialog
      title="Acknowledge kill switch"
      onClose={onCancel}
      note="After acknowledging, the agent is Paused; press Resume when ready."
      actions={[
        { label: "Cancel", onClick: onCancel },
        {
          label: pending ? "Acknowledging…" : "Acknowledge and pause",
          variant: "primary",
          disabled: !checked || pending,
          onClick: onAcknowledge,
        },
      ]}
    >
      <div className="kv-box">
        <div className="kv">
          <span>Fired at</span>
          <span className="num">{formatDateTime(kill.at)}</span>
        </div>
        <div className="kv">
          <span>Peak → current equity</span>
          <span className="num">
            {formatEur(kill.peak, 0)} → {formatEur(equity, 0)}
          </span>
        </div>
        <div className="kv">
          <span>Positions closed</span>
          <span className="num">
            {kill.closed.length > 0 ? kill.closed.join(", ") : "none"}
          </span>
        </div>
      </div>
      <label className="check-label">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => setChecked(e.target.checked)}
        />
        <span>
          I understand drawdown will be measured from {formatEur(equity, 0)}{" "}
          from now on.
        </span>
      </label>
    </Dialog>
  );
}
