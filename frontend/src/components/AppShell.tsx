import { useEffect, useState, type ReactNode } from "react";
import {
  useAcknowledgeKill,
  useFlatten,
  useFlattenPreview,
  usePauseAgent,
  useResumeAgent,
  useRunNow,
  useSnapshot,
  useSnapshotStream,
} from "../api/hooks";
import { formatEur, formatPct, formatSignedEur, formatDateTime } from "../lib/format";
import { displayStatus, statusLabel } from "../lib/status";
import { Banner } from "./Banner";
import { Button } from "./Button";
import { Dialog } from "./Dialog";
import { EmptyState } from "./EmptyState";
import { Sidebar } from "./Sidebar";
import { SidePanel } from "./SlidePanel";
import { TopBar } from "./TopBar";

/**
 * Collapsible sidebar + top bar + centred page (max 1440 px). The Ask button
 * opens the read-only Ask side panel over the current screen.
 */
export function AppShell({
  title,
  actions,
  controls = true,
  children,
}: {
  title: string;
  actions?: ReactNode;
  controls?: boolean;
  children?: ReactNode;
}) {
  const { connected } = useSnapshotStream();
  const { data: snapshot, dataUpdatedAt } = useSnapshot();
  const status = displayStatus(connected, snapshot?.status);
  const lastKnown = snapshot ? { status: snapshot.status, at: dataUpdatedAt } : undefined;
  const [askOpen, setAskOpen] = useState(false);
  const [flattenOpen, setFlattenOpen] = useState(false);
  const [killOpen, setKillOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const runNow = useRunNow();
  const pause = usePauseAgent();
  const resume = useResumeAgent();
  const flatten = useFlatten();
  const acknowledge = useAcknowledgeKill();
  const { data: preview } = useFlattenPreview(flattenOpen);

  const killed = snapshot?.status === "killed" && snapshot.kill != null;

  // Browser tab: agent status and today's P&L at a glance.
  useEffect(() => {
    document.title = snapshot
      ? `${statusLabel(status)} · ${formatSignedEur(snapshot.today_pnl)} today — Trading Agent`
      : "Trading Agent";
  }, [status, snapshot]);

  const busy =
    runNow.isPending ||
    pause.isPending ||
    resume.isPending ||
    flatten.isPending;

  const onError = (error: unknown) =>
    setActionError(error instanceof Error ? error.message : String(error));

  return (
    <div className="shell">
      <Sidebar status={status} lastKnown={lastKnown} onAsk={() => setAskOpen(true)} />
      <div className="shell__main">
        <TopBar
          title={title}
          status={status}
          lastKnown={lastKnown}
          running={status === "running"}
          errors24h={snapshot?.errors_24h ?? 0}
          controls={controls}
          controlsDisabled={
            killed ? "Acknowledge the kill first" : busy ? "Working…" : undefined
          }
          actions={actions}
          onRunNow={() => runNow.mutate(undefined, { onError })}
          onTogglePause={() =>
            status === "running"
              ? pause.mutate(undefined, { onError })
              : resume.mutate(undefined, { onError })
          }
          onFlatten={() => setFlattenOpen(true)}
        />
        <main className="page">
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
        </main>
      </div>

      {askOpen && (
        <SidePanel
          label="Ask the agent"
          title="Ask the agent"
          onClose={() => setAskOpen(false)}
        >
          <EmptyState title="Ask the agent" footer="This panel arrives with the Evidence build step." />
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
