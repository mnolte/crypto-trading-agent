import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { Snapshot } from "../api/client";
import { useNow } from "../api/hooks";
import {
  formatEur,
  formatPct,
  formatSignedEur,
  formatTime,
} from "../lib/format";
import type { DisplayStatus } from "../lib/status";
import { Button } from "./Button";
import { RegimeChip } from "./RegimeChip";

/** The shell header (design/DESIGN.md → The shell): title and sub line,
 * PAPER badge, status pill with the next cycle, Needs you chip (hidden at
 * 0), Equity, Today, Regime (BTC), Ask, Pause/Resume, Stop & flatten. */

export function Header({
  title,
  sub,
  status,
  lastKnown,
  snapshot,
  disabledTitle,
  onAsk,
  onTogglePause,
  onFlatten,
}: {
  title: string;
  sub: ReactNode;
  status: DisplayStatus;
  lastKnown?: { status: string; at: number };
  snapshot?: Snapshot;
  disabledTitle?: string;
  onAsk: () => void;
  onTogglePause: () => void;
  onFlatten: () => void;
}) {
  const now = new Date(useNow());
  const needs = snapshot?.needs?.items.length ?? 0;
  const running = status === "running";
  const btcRegime = snapshot?.latest?.["BTC-EUR"]?.regime ?? null;
  const nextRun = snapshot?.next_run_at ? new Date(snapshot.next_run_at) : null;
  const minsLeft = nextRun
    ? Math.max(0, Math.round((nextRun.getTime() - now.getTime()) / 60000))
    : null;

  return (
    <header className="shell-header">
      <div className="shell-header__left">
        <div className="shell-header__titlebox">
          <h1 className="shell-header__title">{title}</h1>
          <div className="shell-header__sub">{sub}</div>
        </div>
        <span className="paper-badge" title="Orders are simulated against Bitvavo prices. Real-money trading is not built.">
          PAPER
        </span>
        <StatusLine status={status} lastKnown={lastKnown} snapshot={snapshot} minsLeft={minsLeft} />
        {needs > 0 && (
          <Link className="needs-chip ta-needs-chip" to="/#needs" title={`${needs} item${needs === 1 ? "" : "s"} need${needs === 1 ? "s" : ""} you`}>
            <WarnIcon />
            <span>Needs you · {needs}</span>
          </Link>
        )}
      </div>
      <div className="shell-header__right">
        <HeaderStat label="Equity" value={snapshot ? formatEur(snapshot.equity) : "—"} />
        <HeaderStat
          label="Today"
          value={snapshot ? formatSignedEur(snapshot.today_pnl) : "—"}
          sub={snapshot ? formatSignedPct(snapshot.today_pnl_pct) : undefined}
          tone={snapshot && Number(snapshot.today_pnl) < 0 ? "down" : "up"}
        />
        <div className="shell-header__stat">
          <span className="shell-header__label" title="BTC regime: 4h trend × volatility. Each pair also has its own.">
            Regime
          </span>
          {btcRegime ? (
            <RegimeChip regime={btcRegime} label="BTC" />
          ) : (
            <span className="regime-chip regime-chip--warm">warming up</span>
          )}
        </div>
        <Button
          variant="secondary" className="btn--ask"
          onClick={onAsk}
          title="Ask the agent about this page (Ctrl+J)"
          aria-keyshortcuts="Control+J Meta+J"
          icon={<AskIcon />}
        >
          Ask
        </Button>
        <Button
          variant="secondary"
          onClick={onTogglePause}
          disabled={!!disabledTitle}
          title={disabledTitle ?? "Pause new entries. Stops, targets and the exit watcher keep working."}
          aria-pressed={!running}
          icon={running ? <PauseIcon /> : <PlayIcon />}
        >
          {running ? "Pause" : "Resume"}
        </Button>
        <Button
          variant="danger"
          onClick={onFlatten}
          disabled={!!disabledTitle}
          title={disabledTitle ?? "Close every position at market and pause"}
          icon={<StopIcon />}
        >
          Stop &amp; flatten
        </Button>
      </div>
    </header>
  );
}

function StatusLine({
  status,
  lastKnown,
  snapshot,
  minsLeft,
}: {
  status: DisplayStatus;
  lastKnown?: { status: string; at: number };
  snapshot?: Snapshot;
  minsLeft: number | null;
}) {
  const labels: Record<DisplayStatus, string> = {
    running: "Running",
    paused: "Paused",
    killed: "Killed",
    error: "Error",
    disconnected: "Disconnected",
  };
  const next = snapshot?.next_run_at ? new Date(snapshot.next_run_at) : null;
  return (
    <div
      className={`status-pill status-pill--${status}`}
      title={
        status === "disconnected" && lastKnown
          ? `Last known: ${lastKnown.status}, ${formatTime(new Date(lastKnown.at).toISOString())}`
          : "Cycles run every 15 minutes, day and night"
      }
    >
      <span className="status-pill__dot" />
      <span>{labels[status]}</span>
      {status === "running" && next && (
        <span className="status-pill__muted">
          · next cycle {formatTime(next.toISOString())}
          {minsLeft != null && ` · in ${minsLeft} min`}
        </span>
      )}
      {status === "disconnected" && lastKnown && (
        <span className="status-pill__muted">
          · last known {formatTime(new Date(lastKnown.at).toISOString())}
        </span>
      )}
    </div>
  );
}

function HeaderStat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "up" | "down";
}) {
  return (
    <div className="shell-header__stat">
      <span className="shell-header__label">{label}</span>
      <span className={`shell-header__value${tone ? ` shell-header__value--${tone}` : ""}`}>
        {value}
        {sub && <span className="shell-header__valuesub"> {sub}</span>}
      </span>
    </div>
  );
}

function formatSignedPct(pct: string): string {
  const negative = pct.startsWith("-");
  const magnitude = negative ? pct.slice(1) : pct;
  return `${negative ? "−" : "+"}${formatPct(magnitude, 2)}`;
}

/** The sticky phone top bar (<820 px): app mark, screen title, Needs you
 * count, Ask and a menu button that opens the sidebar as an overlay. */
export function PhoneBar({
  title,
  needs,
  menuOpen,
  onAsk,
  onMenu,
}: {
  title: string;
  needs: number;
  menuOpen: boolean;
  onAsk: () => void;
  onMenu: () => void;
}) {
  return (
    <div className="ta-phonebar">
      <div className="phonebar__mark">TA</div>
      <span className="phonebar__title">{title}</span>
      {needs > 0 && (
        <Link className="needs-chip needs-chip--icon" to="/#needs" aria-label={`${needs} items need you`}>
          <WarnIcon />
          {needs}
        </Link>
      )}
      <button type="button" className="phonebar__btn phonebar__btn--ask" aria-label="Ask the agent" onClick={onAsk}>
        <AskIcon size={20} />
      </button>
      <button
        type="button"
        className="phonebar__btn"
        aria-label="Open menu"
        aria-expanded={menuOpen}
        onClick={onMenu}
      >
        <MenuIcon />
      </button>
    </div>
  );
}

function WarnIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 14px" }}>
      <path d="M12 3l10 18H2z" />
      <line x1="12" y1="10" x2="12" y2="14" />
      <line x1="12" y1="18" x2="12" y2="18" />
    </svg>
  );
}

function AskIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 auto" }}>
      <path d="M4 5h16v11H9l-5 4z" />
      <line x1="8" y1="9" x2="16" y2="9" />
      <line x1="8" y1="12" x2="13" y2="12" />
    </svg>
  );
}

function PauseIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="8" y1="5" x2="8" y2="19" />
      <line x1="16" y1="5" x2="16" y2="19" />
    </svg>
  );
}

function PlayIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polygon points="7 5 19 12 7 19" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <line x1="12" y1="7" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12" y2="17" />
    </svg>
  );
}

function MenuIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 20px" }}>
      <line x1="4" y1="7" x2="20" y2="7" />
      <line x1="4" y1="12" x2="20" y2="12" />
      <line x1="4" y1="17" x2="20" y2="17" />
    </svg>
  );
}

