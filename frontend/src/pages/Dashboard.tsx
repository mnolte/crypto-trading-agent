import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { UTCTimestamp } from "lightweight-charts";
import type {
  BriefingOut,
  CycleDecisions,
  CycleSlot,
  CycleSlotChange,
  CycleSlotState,
  Decision,
  EntryPause,
  MarketOut,
  Position,
  RiskBudget,
  SettingsOut,
  Snapshot,
  Tf,
} from "../api/client";
import {
  useBriefings,
  useCandles,
  useCyclesRecent,
  useIndicators,
  useMarkets,
  useNow,
  usePendingAction,
  useRunNow,
  useSettings,
  useSnapshot,
  useSources,
  useTrades,
} from "../api/hooks";
import { ActionBadge } from "../components/ActionBadge";
import { AppShell } from "../components/AppShell";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { PriceChart, type PriceLine, type PriceMarker, type Candle } from "../components/Charts";
import { Chip } from "../components/Chip";
import { DecisionDrawer } from "../components/DecisionDrawer";
import { EmptyState } from "../components/EmptyState";
import { GlossaryText } from "../components/Glossary";
import { GlossaryTerm } from "../components/GlossaryTerm";
import { Meter } from "../components/Meter";
import { PauseSelect } from "../components/PauseSelect";
import { RegimeChip } from "../components/RegimeChip";
import { SegmentedControl } from "../components/SegmentedControl";
import { SignalChip } from "../components/SignalChip";
import { CardFetch } from "../lib/cardState";
import {
  formatAge,
  formatChange,
  formatCountdown,
  formatDayLong,
  formatDayShort,
  formatEur,
  formatPct,
  formatPrice,
  formatQty,
  formatSignedEur,
  formatTime,
  formatTimeUtc,
} from "../lib/format";

const FIRST_RUN_KEY = "ta.firstRun.dismissed";
const TFS: Tf[] = ["1h", "4h", "1d"];
const PHONE_SLOTS = 9 * 4; // last 6 h plus the next 3

function usePhoneWidth(): boolean {
  const [phone, setPhone] = useState(
    () => window.matchMedia("(max-width: 820px)").matches,
  );
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 820px)");
    const on = () => setPhone(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return phone;
}

export function Dashboard() {
  const snapshotQuery = useSnapshot();
  const { data: snapshot } = snapshotQuery;
  const { data: settings } = useSettings();
  const { data: sources } = useSources();
  const { data: markets } = useMarkets();
  const now = useNow();
  const [searchParams] = useSearchParams();

  const [pair, setPair] = useState<string | null>(null);
  const [tf, setTf] = useState<Tf>("1h");
  const [decisionId, setDecisionId] = useState<string | null>(null);
  const chartRef = useRef<HTMLElement>(null);
  // Deep link: /?decision=<id> (Ask citations, weekly review) opens the drawer.
  const decisionParam = searchParams.get("decision");
  const [prevDecisionParam, setPrevDecisionParam] = useState<string | null>(null);
  if (decisionParam !== prevDecisionParam) {
    setPrevDecisionParam(decisionParam);
    if (decisionParam) setDecisionId(decisionParam);
  }
  const [firstRunDismissed, setFirstRunDismissed] = useState(
    () => localStorage.getItem(FIRST_RUN_KEY) === "1",
  );

  const active = markets?.filter((m) => m.active) ?? [];
  const marketIds = active.map((m) => m.market);
  const heldPairs = (snapshot?.positions ?? []).map((p) => p.pair);
  const selected = pair ?? heldPairs[0] ?? marketIds[0] ?? null;

  const price = selected ? snapshot?.prices[selected] : undefined;
  const openai = sources?.find((s) => s.id === "openai");
  const showFirstRun = openai !== undefined && !openai.key_set && !firstRunDismissed;
  const paused = new Map((snapshot?.paused ?? []).map((p) => [p.pair, p]));

  const pickPair = (id: string, scroll = false) => {
    setPair(id);
    if (scroll) chartRef.current?.scrollIntoView({ block: "start" });
  };

  const today = formatDayLong(new Date(now).toISOString());
  const time = formatTime(new Date(now).toISOString());

  return (
    <AppShell title="Dashboard" sub={`${today} · ${time} Amsterdam`}>
      {showFirstRun && (
        <Banner
          variant="info"
          actions={
            <>
              <Link to="/settings" className="btn btn--primary">
                Add OpenAI key
              </Link>
              <Button
                variant="secondary"
                onClick={() => {
                  localStorage.setItem(FIRST_RUN_KEY, "1");
                  setFirstRunDismissed(true);
                }}
              >
                Start with Rules only
              </Button>
            </>
          }
        >
          Welcome. The agent is paused and will decide with the Rules baseline
          until you add an OpenAI key. Paper balance:{" "}
          {formatEur(settings?.paper.start_balance_eur ?? 10000, 0)}.
        </Banner>
      )}
      {price?.stale && selected && (
        <Banner
          variant="warning"
          actions={
            <Link to={`/indicators?pair=${selected}`} className="btn btn--secondary btn--sm">
              Data health
            </Link>
          }
        >
          {selected} data is stale: its newest candles are beyond the freshness
          limit. The agent holds {selected}; its stop and target are still
          watched.
        </Banner>
      )}

      <NeedsYouCard snapshot={snapshot} query={snapshotQuery} />

      {(snapshot?.pending.length ?? 0) > 0 && (
        <PendingApprovals
          decisions={snapshot?.pending ?? []}
          mode={settings?.execution_mode ?? "auto"}
          vetoWindowSeconds={settings?.veto_window_s ?? 60}
          now={now}
          onOpen={setDecisionId}
        />
      )}

      <CyclesCard snapshot={snapshot} snapshotQuery={snapshotQuery} quietOn={settings?.learning.quiet_line ?? true} />

      <section aria-label="Account summary" className="tiles-row">
        <ModelCallsTile snapshot={snapshot} />
        <SinceStartTile snapshot={snapshot} />
      </section>

      <div className="row">
        <PositionsCard snapshot={snapshot} markets={markets} settings={settings} query={snapshotQuery} onOpen={setDecisionId} />
        <RiskBudgetCard budget={snapshot?.risk_budget} settings={settings} snapshot={snapshot} />
      </div>

      <div className="row">
        <PriceChartCard
          anchorRef={chartRef}
          snapshot={snapshot}
          pair={selected}
          heldPairs={heldPairs}
          marketIds={marketIds}
          tf={tf}
          onTf={setTf}
          onPick={(id) => pickPair(id)}
        />
        <LatestDecisionCard
          decision={selected ? (snapshot?.latest[selected] ?? null) : null}
          pair={selected}
          pause={selected ? (paused.get(selected) ?? paused.get("*")) : undefined}
          onOpen={setDecisionId}
        />
      </div>

      <div className="row">
        <DecisionsCard onOpen={setDecisionId} />
        <BriefingCard at={settings?.learning.briefing_time ?? "08:00"} />
      </div>

      <MarketsCard
        markets={active}
        heldPairs={heldPairs}
        selected={selected}
        paused={paused}
        onPick={(id) => pickPair(id, true)}
      />

      {decisionId && (
        <DecisionDrawer
          id={decisionId}
          onNavigate={(id) => setDecisionId(id)}
          onClose={() => setDecisionId(null)}
        />
      )}
    </AppShell>
  );
}

/* ---------------------------------------------------------- Needs you -- */

function NeedsYouCard({
  snapshot,
  query,
}: {
  snapshot?: Snapshot;
  query: ReturnType<typeof useSnapshot>;
}) {
  const needs = snapshot?.needs;
  const items = needs?.items ?? [];
  return (
    <section id="needs" aria-label="Needs you" className="needs-card">
      <CardFetch query={query} name="needs">
        {items.length > 0 && (
          <div className="needs-card__head">
            <h2 className="needs-card__title">
              Needs you <span className="needs-card__count">{items.length}</span>
            </h2>
            <span className="needs-card__hint">
              most urgent first · items leave when they&apos;re done
            </span>
          </div>
        )}
        {items.map((item) => (
          <div className="needs-card__row" key={`${item.kind}-${item.title}`}>
            <span className="needs-card__text">
              <span className="needs-card__icon" aria-hidden="true">
                <WarnIcon />
              </span>
              <span>
                <b>{item.title}</b>{" "}
                {item.detail && <span className="needs-card__detail">{item.detail}</span>}
              </span>
            </span>
            <Link to={item.action_target} className="needs-card__action">
              {item.action_label}
            </Link>
          </div>
        ))}
        <div className="needs-card__health">
          {(needs?.health ?? []).map((h) => (
            <span className="health-item" key={h.key}>
              <span className={`health-item__dot health-item__dot--${h.state}`} />
              <span className="health-item__label">{h.label}</span>{" "}
              <span className={`health-item__value health-item__value--${h.state}`}>
                {h.value ?? "—"}
              </span>
            </span>
          ))}
        </div>
      </CardFetch>
    </section>
  );
}

function WarnIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3l10 18H2z" />
      <line x1="12" y1="10" x2="12" y2="14" />
      <line x1="12" y1="18" x2="12" y2="18" />
    </svg>
  );
}

/* --------------------------------------------------------------- Cycles -- */

function CyclesCard({
  snapshot,
  snapshotQuery,
  quietOn,
}: {
  snapshot?: Snapshot;
  snapshotQuery: ReturnType<typeof useSnapshot>;
  quietOn: boolean;
}) {
  const cycles = useCyclesRecent(24);
  const runNow = useRunNow();
  const now = useNow();
  const phone = usePhoneWidth();
  const [runError, setRunError] = useState<string | null>(null);

  const slots = cycles.data?.slots ?? [];
  const nextIdx = slots.findIndex((s) => s.state === "next");
  const visible = phone && nextIdx >= PHONE_SLOTS ? slots.slice(nextIdx - PHONE_SLOTS + 12) : slots;

  const nextRun = snapshot?.next_run_at ? new Date(snapshot.next_run_at) : null;
  const countdown = nextRun ? Math.max(0, (nextRun.getTime() - now) / 1000) : null;
  const progress =
    countdown === null ? null : Math.min(100, Math.max(0, (1 - countdown / 900) * 100));

  const summary = cycles.data?.summary;
  const age = summary?.running_since
    ? formatAge(Math.max(0, (now - new Date(summary.running_since).getTime()) / 1000))
    : null;

  return (
    <section aria-label="Cycles" className="card cycles-card">
      <div className="cycles-card__top">
        <CardFetch query={snapshotQuery} name="quiet line">
          {quietOn && snapshot?.quiet && (
            <div className="cycles-card__quiet">
              <span className="cycles-card__chip">
                {summary?.running_since
                  ? `${formatTime(lastRan(slots)?.at ?? snapshot.next_run_at ?? "")} cycle`
                  : "cycle"}
              </span>
              <span className="cycles-card__quiettext">
                <GlossaryText text={snapshot.quiet} />{" "}
                <Link to="/insights">Why?</Link>
              </span>
            </div>
          )}
        </CardFetch>
        <div role="timer" aria-label="Next cycle" className="cycles-card__next">
          <div className="cycles-card__nextline">
            <span>
              Next cycle{" "}
              <b className="num">{nextRun ? formatTime(nextRun.toISOString()) : "—"}</b>{" "}
              <span className="card__meta">
                · {nextRun ? `${formatTimeUtc(nextRun.toISOString())} UTC` : ""}
              </span>
            </span>
            <span className="num cycles-card__countdown">
              {snapshot?.cycle_in_progress
                ? "running"
                : countdown !== null
                  ? formatCountdown(countdown)
                  : "paused"}
            </span>
          </div>
          <div className="cycles-card__bar" aria-hidden="true">
            <div style={{ width: `${progress ?? 0}%` }} />
          </div>
          <div className="cycles-card__nextfoot">
            <span>every 15 min, day and night</span>
            <button
              type="button"
              className="cycles-card__run"
              title="Run one cycle now. The 15-minute schedule keeps its times."
              disabled={runNow.isPending}
              onClick={() =>
                runNow.mutate(undefined, {
                  onError: (e) =>
                    setRunError(e instanceof Error ? e.message : String(e)),
                })
              }
            >
              {runNow.isPending ? "Running…" : "Run now"}
            </button>
          </div>
          {runError && <span className="field__msg">{runError}</span>}
        </div>
      </div>

      <div className="cycles-card__timeline">
        <div className="cycles-card__headrow">
          <h2 className="card__title">
            <span className="ta-hide-phone">
              Last 24 hours{" "}
              <span className="cycles-card__titlesub">· and the next 3</span>
            </span>
            <span className="ta-only-phone">Last 6 hours</span>
          </h2>
          {summary && (
            <span className="cycles-card__summary">
              <span className="num">{summary.ran}</span> of {summary.scheduled}{" "}
              cycles ran in 24 h
              {summary.missed.length > 0 && (
                <>
                  {" · "}
                  {summary.missed.map((m, i) => (
                    <span key={i}>
                      <span className="num">{m.count}</span> missed{" "}
                      {formatTime(m.from_at)}–{formatTime(m.to_at)}
                      {m.reason ? ` (${m.reason})` : ""}
                    </span>
                  ))}
                </>
              )}
              {summary.errors > 0 && (
                <>
                  {" · "}
                  <Link
                    to={`/logs?cycle_id=${encodeURIComponent(String(summary.cycle_id))}`}
                    className="cycles-card__errors"
                  >
                    <span className="num">{summary.errors}</span> errors
                  </Link>
                </>
              )}
              {" · cycle #"}
              {summary.cycle_id}
              {age ? `, running ${age}` : ""}
            </span>
          )}
        </div>
        <CardFetch query={cycles} name="cycle timeline" emptyWhen={slots.length === 0 && !cycles.isPending} empty={<EmptyState title="No cycles yet" footer="The timeline fills in once the scheduler has run a cycle." />}>
          <CycleTimeline slots={visible} nextIdx={nextIdx} />
          <div className="cycles-card__legend">
            <Legend><i className="slot-tick slot-tick--no_change" />no change</Legend>
            <Legend><i className="slot-letter">B</i><i className="slot-tick slot-tick--entry" />entry</Legend>
            <Legend><i className="slot-letter">S</i><i className="slot-tick slot-tick--exit_gain" />exit (green gain, red loss)</Legend>
            <Legend><i className="slot-letter">↑</i><i className="slot-tick slot-tick--stop_moved" />stop moved</Legend>
            <Legend><i className="slot-letter">!</i><i className="slot-tick slot-tick--error" />error, Rules answered</Legend>
            <Legend><i className="slot-tick slot-tick--missed" />missed</Legend>
            <Legend><i className="slot-tick slot-tick--next" />next</Legend>
            {slots.some((s) => s.jobs.length > 0) && (
              <Legend><i className="slot-job" />scheduled job</Legend>
            )}
          </div>
        </CardFetch>
      </div>
    </section>
  );
}

function lastRan(slots: CycleSlot[]): CycleSlot | undefined {
  const past = slots.filter((s) => s.state !== "next" && s.state !== "future");
  return past.at(-1);
}

function Legend({ children }: { children: React.ReactNode }) {
  return <span className="cycles-card__legenditem">{children}</span>;
}

const SLOT_LETTER: Partial<Record<CycleSlotState, { letter: string; cls: string }>> = {
  entry: { letter: "B", cls: "slot-letter--up" },
  exit_gain: { letter: "S", cls: "slot-letter--up" },
  exit_loss: { letter: "S", cls: "slot-letter--down" },
  stop_moved: { letter: "↑", cls: "slot-letter--info" },
  error: { letter: "!", cls: "slot-letter--warn" },
};

function slotTooltip(slot: CycleSlot): string {
  const when = `${formatTime(slot.at)} · ${formatTimeUtc(slot.at)} UTC`;
  const lines = [when];
  if (slot.state === "missed")
    lines.push(`missed${slot.missed_reason ? ` (${slot.missed_reason})` : ""}`);
  if (slot.state === "paused") lines.push("paused");
  if (slot.state === "next") lines.push("next cycle");
  for (const c of slot.changes) lines.push(`${c.kind}: ${c.detail}`);
  for (const j of slot.jobs) lines.push(`job: ${j}`);
  if (slot.cycle != null) lines.push(`cycle #${slot.cycle}`);
  return lines.join("\n");
}

function CycleTimeline({ slots }: { slots: CycleSlot[]; nextIdx: number }) {
  // Time labels every 3 h (12 slots), plus the date at each midnight.
  return (
    <div role="img" className="slotline" aria-label="Cycles over the last 24 hours">
      {slots.map((slot, i) => {
        const d = new Date(slot.at);
        const mins = d.getUTCHours() * 60 + d.getUTCMinutes();
        const showTime = mins % 180 === 0;
        const showDate = mins === 0;
        const mark = SLOT_LETTER[slot.state];
        return (
          <span
            key={slot.at}
            className={`slot${i % 2 ? " slot--alt" : ""}`}
            title={slotTooltip(slot)}
          >
            <span className={`slot-letter ${mark?.cls ?? ""}`} aria-hidden="true">
              {mark?.letter ?? ""}
            </span>
            <span className={`slot-tick slot-tick--${slot.state}`} />
            {slot.jobs.length > 0 && <span className="slot-job" />}
            <span className="slot-time" aria-hidden="true">
              {showDate ? formatDayShort(slot.at) : showTime ? formatTime(slot.at) : ""}
            </span>
          </span>
        );
      })}
    </div>
  );
}

/* ---------------------------------------------------------------- Tiles -- */

function ModelCallsTile({ snapshot }: { snapshot?: Snapshot }) {
  const usage = snapshot?.usage;
  const pct = usage && usage.daily_budget > 0 ? (usage.calls_today / usage.daily_budget) * 100 : 0;
  return (
    <Link to="/inspector" className="ta-tile tile" aria-label="Model calls today, open the LLM inspector">
      <span
        className="tile__label gterm"
        title="Decision calls since 00:00 Amsterdam: every held position is reviewed each cycle, plus pairs that pass the pre-filter."
      >
        Model calls today
      </span>
      <span className="tile__value num">{usage ? usage.calls_today : "—"}</span>
      <span className="tile__bar" aria-hidden="true">
        <span style={{ width: `${Math.min(100, pct)}%` }} />
      </span>
      <span className="tile__sub">
        {usage
          ? `of ${usage.daily_budget.toLocaleString("en-GB")} max · ${formatEur(usage.cost_today_eur)} today · ${formatEur(usage.cost_month_eur)} of ${formatEur(usage.cap_eur, 0)} this month`
          : "—"}
      </span>
      <span className="tile__open">Open →</span>
    </Link>
  );
}

function SinceStartTile({ snapshot }: { snapshot?: Snapshot }) {
  const ret = snapshot?.return_since_start_pct;
  const btc = snapshot?.btc_return_since_start_pct;
  return (
    <Link to="/performance" className="ta-tile tile" aria-label="Since start, open Performance">
      <span className="tile__label">Since start</span>
      <span className={`tile__value num ${ret != null && ret < 0 ? "down" : "up"}`}>
        {ret != null ? formatChange(ret) : "—"}
      </span>
      <span className="tile__sub">
        Holding BTC {btc != null ? formatChange(btc) : "—"}
        {snapshot?.account_started_at
          ? ` · started ${formatDayShort(snapshot.account_started_at)}`
          : ""}
      </span>
      <span className="tile__open">Open →</span>
    </Link>
  );
}

/* ------------------------------------------------------------- Positions -- */

function PositionsCard({
  snapshot,
  markets,
  settings,
  query,
  onOpen,
}: {
  snapshot?: Snapshot;
  markets?: MarketOut[];
  settings?: SettingsOut;
  query: ReturnType<typeof useSnapshot>;
  onOpen: (id: string) => void;
}) {
  const now = useNow(30_000);
  const positions = snapshot?.positions ?? [];
  // Display-only total; money is never computed for decisions on the frontend.
  const unrealised = positions.reduce((s, p) => s + Number(p.unrealised), 0);
  const passing = (markets ?? []).filter((m) => m.rules_state === "passed").length;
  const nearAtr = settings?.ui?.stop_near_atr ?? 1.5;
  return (
    <Card
      className="row__main"
      label={`Open positions · ${positions.length}`}
      title={
        <Link to="/positions" className="card__link">
          {positions.length > 0 ? `Open positions · ${positions.length}` : "Open positions"}
        </Link>
      }
      actions={
        <span className="card__meta" style={{ fontSize: "var(--ta-text-md)" }}>
          {positions.length > 0 && (
            <>
              unrealised{" "}
              <span className="num" style={{ color: unrealised >= 0 ? "var(--ta-positive)" : "var(--ta-negative-text)" }}>
                {formatSignedEur(unrealised)}
              </span>{" "}
              ·{" "}
            </>
          )}
          <Link to="/performance">Performance</Link>
        </span>
      }
    >
      <CardFetch
        query={query}
        name="positions"
        emptyWhen={snapshot !== undefined && positions.length === 0}
        empty={
          <EmptyState title="No open positions.">
            {snapshot?.next_run_at
              ? `The next cycle at ${formatTime(snapshot.next_run_at)} may open one${passing > 0 ? `: ${passing} pair${passing === 1 ? "" : "s"} passes the pre-filter` : ""}.`
              : "New BUYs appear here after a decision executes."}
          </EmptyState>
        }
      >
        {positions.map((p) => (
          <PositionRow key={p.pair} p={p} now={now} onOpen={() => onOpen(p.decision_id)} />
        ))}
        {positions.length > 0 && (
          <div className="positions-note">
            Bar: stop (red) to target (green), entry tick, white dot for the
            last price. Under {formatQty(String(nearAtr))} ATR (1h) to the stop
            the distance turns amber. Stops and targets are watched by the
            agent every few seconds; the exit watcher keeps running when the
            agent is paused.
          </div>
        )}
      </CardFetch>
    </Card>
  );
}

function PositionRow({ p, now, onOpen }: { p: Position; now: number; onOpen: () => void }) {
  const entry = Number(p.entry);
  const stop = Number(p.stop);
  const target = Number(p.target);
  const last = Number(p.last);
  const lo = Math.min(stop, entry);
  const hi = Math.max(target, last, entry);
  const span = hi - lo || 1;
  const pct = (v: number) => Math.max(0, Math.min(100, ((v - lo) / span) * 100));
  const entryPct = pct(entry);
  const lastPct = pct(last);
  const fillLeft = Math.min(entryPct, lastPct);
  const fillWidth = Math.abs(lastPct - entryPct);
  const win = last >= entry;
  const pnl = Number(p.unrealised);
  const nearCls = p.stop_near ? "pos__dist pos__dist--near" : "pos__dist";
  return (
    <button type="button" className="pos ta-row" onClick={onOpen}>
      <span className="pos__head">
        <b>{p.pair}</b>{" "}
        <span className="pos__meta">
          {formatQty(p.qty)} · {formatAge(Math.max(0, (now - new Date(p.opened_at).getTime()) / 1000))}
        </span>{" "}
        <span className={`num ${nearCls}`}>
          · {p.stop_dist_pct != null ? `${formatQty(p.stop_dist_pct)}%` : "—"} ·{" "}
          {p.stop_dist_atr != null ? `${formatQty(p.stop_dist_atr)} ATR` : "—"} to stop
        </span>
      </span>
      <span className="pos__pnl num">
        <span style={{ color: pnl >= 0 ? "var(--ta-positive)" : "var(--ta-negative-text)" }}>
          {formatSignedEur(p.unrealised)}
        </span>{" "}
        {p.stop_above_entry ? (
          <span className="pos__tag">stop above entry</span>
        ) : (
          <span className="pos__r">{p.pnl_r != null ? `${formatQty(p.pnl_r)}R` : ""}</span>
        )}
      </span>
      <span className="pos__barwrap">
        <span className="pos__bar" aria-hidden="true">
          <i className="pos__track" />
          <i
            className="pos__fill"
            style={{
              left: `${fillLeft}%`,
              width: `${Math.max(0.4, fillWidth)}%`,
              background: win ? "var(--ta-positive)" : "var(--ta-negative)",
            }}
          />
          <i className="pos__tick pos__tick--stop" title="Stop" style={{ left: `calc(${pct(stop)}% - 1px)` }} />
          <i className="pos__tick pos__tick--entry" title="Entry" style={{ left: `calc(${entryPct}% - 1px)` }} />
          <i className="pos__tick pos__tick--target" title="Target" />
          <i className="pos__dot" title="Last price" style={{ left: `calc(${lastPct}% - 5px)` }} />
        </span>
        <span className="pos__labels num">
          <span className="pos__stop">{formatPrice(stop)}</span>
          <span>
            entry {formatPrice(entry)} · last {formatPrice(last)}
          </span>
          <span className="pos__target">{formatPrice(target)}</span>
        </span>
      </span>
    </button>
  );
}

/* ------------------------------------------------------------ Risk budget -- */

function RiskBudgetCard({
  budget,
  settings,
  snapshot,
}: {
  budget?: RiskBudget;
  settings?: SettingsOut;
  snapshot?: Snapshot;
}) {
  const { data: trades } = useTrades();
  const now = useNow(30_000);
  if (!budget) return null;
  const openRisk = Number(budget.open_risk_pct);
  const openCap = Number(budget.open_risk_cap_pct);
  const exposure = snapshot?.exposure_pct ?? 0;
  const exposureCap = Number(settings?.risk.max_exposure_pct ?? 100);
  const todayLoss = Number(budget.today_loss_pct);
  const dailyLimit = Number(budget.daily_limit_pct);
  const drawdown = Number(budget.drawdown_pct);
  const kill = Number(budget.kill_pct);
  const corrMax = settings?.risk.correlated_max_positions;

  const cooldownH = Number(settings?.risk.cooldown_after_stopout_hours ?? 6);
  const cooldowns = (trades?.items ?? [])
    .filter((t) => t.exit_reason === "stop")
    .filter((t) => now - new Date(t.closed_at).getTime() < cooldownH * 3_600_000)
    .map((t) => ({
      pair: t.pair,
      until: new Date(new Date(t.closed_at).getTime() + cooldownH * 3_600_000),
    }));

  const room =
    budget.full_trades_left <= 0
      ? "none"
      : `${budget.full_trades_left} more trade${budget.full_trades_left === 1 ? "" : "s"} · ${budget.room_limited_by === "cash" ? "cash" : "risk"}-limited`;

  return (
    <Card
      className="row__side"
      title="Risk budget"
      label="Risk budget"
      actions={
        <Link to="/settings" className="card__link">
          Limits in Settings
        </Link>
      }
    >
      <div className="risk-grid">
        <div className="risk-col">
          <RiskMeter
            label={<GlossaryTerm term="open risk">Open risk</GlossaryTerm>}
            tip="What you would lose if every open position hit its stop at the same time."
            value={`${openRisk.toFixed(1)}% of ${formatPct(openCap, 1)} cap`}
            pct={openCap > 0 ? (openRisk / openCap) * 100 : 0}
            color="accent"
          />
          <RiskMeter
            label={<GlossaryTerm term="exposure">Exposure</GlossaryTerm>}
            tip="Share of your equity held in coins rather than euros. Spot only, no leverage."
            value={`${formatPct(exposure)} of ${formatPct(exposureCap)} cap`}
            pct={exposureCap > 0 ? (exposure / exposureCap) * 100 : 0}
            color="neutral"
          />
          <RiskMeter
            label="Today's loss"
            tip={`Loss since 00:00 Amsterdam. At −${formatPct(dailyLimit)} no new entries until midnight.`}
            value={`${todayLoss.toFixed(1)}% of −${formatPct(dailyLimit)} limit`}
            pct={dailyLimit > 0 ? (todayLoss / dailyLimit) * 100 : 0}
            color="neutral"
          />
          <RiskMeter
            label={<GlossaryTerm term="drawdown">Drawdown</GlossaryTerm>}
            tip={`How far equity is below its highest point so far. At −${formatPct(kill)} the kill switch closes everything.`}
            value={`−${drawdown.toFixed(1)}% of −${formatPct(kill)} kill`}
            pct={kill > 0 ? (drawdown / kill) * 100 : 0}
            color="neutral"
          />
        </div>
        <div className="risk-rows">
          <RiskRow label="Room for" tip="The smallest of: open risk left ÷ risk per trade, and cash ÷ minimum order." value={room} />
          <RiskRow
            label="Correlated"
            tip={`At most ${corrMax ?? 2} open positions that move together.`}
            value={
              budget.correlated_detail
                ? `${budget.correlated_detail} · ${corrMax != null ? `${corrMax - budget.correlated_slots_left} of ${corrMax}` : ""}`
                : `${budget.correlated_slots_left} slots left`
            }
          />
          <RiskRow
            label="Risk per trade"
            tip="After 3 losses in a row risk per trade is halved until a win."
            value={`${budget.risk_pct_now}% · ${budget.throttle ? "throttle on" : "throttle off"}`}
          />
          <RiskRow
            label="Shock brake"
            tip="No new entries for 2 hours when BTC falls sharply within 30 minutes."
            value={
              budget.shock_brake_until
                ? `on until ${formatTime(budget.shock_brake_until)}`
                : "off"
            }
          />
          <RiskRow
            label="Cooldown"
            tip={`A pair that hit its stop is not bought again for ${formatQty(String(cooldownH))} hours.`}
            value={
              cooldowns.length > 0
                ? cooldowns
                    .map((c) => `${c.pair} until ${formatTime(c.until.toISOString())}`)
                    .join(", ")
                : "none"
            }
          />
        </div>
      </div>
    </Card>
  );
}

function RiskMeter({
  label,
  tip,
  value,
  pct,
  color,
}: {
  label: React.ReactNode;
  tip: string;
  value: string;
  pct: number;
  color: "accent" | "neutral";
}) {
  return (
    <div className="risk-line">
      <div className="risk-line__head">
        <span className="gterm" title={tip}>{label}</span>
        <span className="num">{value}</span>
      </div>
      <Meter pct={pct} color={color === "accent" ? "accent" : "muted"} large />
    </div>
  );
}

function RiskRow({ label, tip, value }: { label: string; tip: string; value: string }) {
  return (
    <div className="risk-row">
      <span className="risk-row__label gterm" title={tip}>{label}</span>
      <span className="risk-row__value">{value}</span>
    </div>
  );
}

/* ------------------------------------------------------------ Price chart -- */

function PriceChartCard({
  anchorRef,
  snapshot,
  pair,
  heldPairs,
  marketIds,
  tf,
  onTf,
  onPick,
}: {
  anchorRef: React.RefObject<HTMLElement | null>;
  snapshot?: Snapshot;
  pair: string | null;
  heldPairs: string[];
  marketIds: string[];
  tf: Tf;
  onTf: (tf: Tf) => void;
  onPick: (id: string) => void;
}) {
  const [findOpen, setFindOpen] = useState(false);
  const [query, setQuery] = useState("");
  const findRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!findOpen) return;
    const onDown = (e: MouseEvent) => {
      if (findRef.current && !findRef.current.contains(e.target as Node)) setFindOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFindOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [findOpen]);

  const feed = useCandles(pair, tf, 72);
  const indicators = useIndicators(pair, tf);
  const price = pair ? snapshot?.prices[pair] : undefined;
  const position = pair ? snapshot?.positions.find((p) => p.pair === pair) : undefined;

  const candles: Candle[] = (feed.data?.candles ?? []).map((c) => ({
    time: (new Date(c.time).getTime() / 1000) as UTCTimestamp,
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
    volume: c.volume,
  }));
  const markers: PriceMarker[] = (feed.data?.markers ?? []).map((m) => ({
    time: (new Date(m.time).getTime() / 1000) as UTCTimestamp,
    kind: m.kind,
    text: m.kind === "buy" ? "B" : m.kind === "sell" ? "S" : "D",
  }));
  const lines: PriceLine[] = position
    ? [
        { price: Number(position.entry), color: "muted", title: "entry" },
        { price: Number(position.stop), color: "down", title: "stop" },
        { price: Number(position.target), color: "up", title: "target" },
      ]
    : [];
  const overlay = (key: "ema20" | "ema50" | "vwap") => ({
    color: key,
    data: (indicators.data?.series[key] ?? [])
      .filter((p) => p.value != null)
      .map((p) => ({
        time: (new Date(p.time).getTime() / 1000) as UTCTimestamp,
        value: p.value ?? 0,
      })),
  });

  const filtered = marketIds.filter((m) => m.toLowerCase().includes(query.toLowerCase()));
  const tfDays = tf === "1h" ? 3 : tf === "4h" ? 12 : 72;
  const chg = Number(price?.chg_24h_pct ?? 0);

  return (
    <section aria-label="Price chart" className="card row__main chart-card" ref={anchorRef}>
      <div className="chart-card__head">
        <h2 className="chart-card__pair">{pair ?? "—"}</h2>
        <span className="chart-card__price num">{price ? formatPrice(price.last) : "—"}</span>
        {price && (
          <span className={`num chart-card__chg ${chg >= 0 ? "up" : "down"}`}>
            {formatChange(price.chg_24h_pct)} 24h
          </span>
        )}
        <span style={{ flex: 1 }} />
        <div role="group" aria-label="Pick a pair" className="chart-card__pairs">
          {heldPairs.map((id) => (
            <button
              key={id}
              type="button"
              aria-pressed={id === pair}
              className={`held-btn${id === pair ? " held-btn--on" : ""}`}
              onClick={() => onPick(id)}
            >
              {id}
              <span className="held-btn__tag">held</span>
            </button>
          ))}
          <div className="find-pair" ref={findRef}>
            <button
              type="button"
              aria-haspopup="listbox"
              aria-expanded={findOpen}
              className="find-pair__btn"
              onClick={() => setFindOpen((v) => !v)}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="11" cy="11" r="6" />
                <line x1="16" y1="16" x2="21" y2="21" />
              </svg>
              Find pair
            </button>
            {findOpen && (
              <div role="listbox" aria-label="Find a pair" className="find-pair__menu">
                <input
                  type="search"
                  aria-label="Search pairs"
                  placeholder="Find a pair"
                  className="find-pair__search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  autoFocus
                />
                {filtered.map((m) => (
                  <button
                    key={m}
                    type="button"
                    role="option"
                    aria-selected={m === pair}
                    className={`pairtab-option${m === pair ? " pairtab-option--on" : ""}`}
                    onClick={() => {
                      onPick(m);
                      setFindOpen(false);
                    }}
                  >
                    {m}
                  </button>
                ))}
                {filtered.length === 0 && (
                  <span className="empty__body" style={{ padding: "8px 10px" }}>No matches</span>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="chart-card__sub">
        <span>
          {tf} candles · last {tfDays} days · Bitvavo · or pick a row in Markets
        </span>
        <SegmentedControl
          flat
          label="Timeframe"
          value={tf}
          onChange={(v) => onTf(v as Tf)}
          options={TFS.map((t) => ({ value: t, label: t }))}
        />
      </div>
      <CardFetch query={feed} name="chart" emptyWhen={feed.data !== undefined && candles.length === 0} empty={<EmptyState title="No candles yet" footer="The chart fills in once candles are stored." />}>
        {candles.length > 0 ? (
          <PriceChart
            candles={candles}
            markers={markers}
            lines={lines}
            overlays={[overlay("ema20"), overlay("ema50"), overlay("vwap")]}
            height={320}
          />
        ) : (
          <EmptyState title="Loading candles…" />
        )}
      </CardFetch>
      <div className="chart-legend">
        <span className="chart-legend__item"><i className="legend-line legend-line--ema20" />EMA 20</span>
        <span className="chart-legend__item"><i className="legend-line legend-line--ema50" />EMA 50</span>
        <span className="chart-legend__item"><i className="legend-line legend-line--vwap" />VWAP (day)</span>
        {position && (
          <>
            <span className="chart-legend__item"><i className="legend-line legend-line--entry" />Entry</span>
            <span className="chart-legend__item"><i className="legend-line legend-line--stop" />Stop</span>
            <span className="chart-legend__item"><i className="legend-line legend-line--target" />Target</span>
          </>
        )}
        <span className="chart-legend__item">
          <span className="chart-marker chart-marker--buy">B</span>Agent buy
        </span>
        <span className="chart-legend__item">
          <span className="chart-marker chart-marker--sell">S</span>Agent sell
        </span>
      </div>
    </section>
  );
}

/* -------------------------------------------------------- Latest decision -- */

function LatestDecisionCard({
  decision,
  pair,
  pause,
  onOpen,
}: {
  decision: Decision | null;
  pair: string | null;
  pause: EntryPause | undefined;
  onOpen: (id: string) => void;
}) {
  return (
    <Card
      className="row__side"
      title="Latest decision"
      label="Latest decision"
      actions={
        decision && (
          <span className="num" style={{ fontSize: 12, color: "var(--ta-text-muted)" }}>
            {formatTime(decision.at)} · {decision.model ?? decision.decider}
          </span>
        )
      }
    >
      {decision ? (
        <>
          <div className="decision-line">
            <ActionBadge action={decision.action} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 16 }}>{decision.pair}</div>
              <div style={{ fontSize: 13, color: "var(--ta-text-muted)" }}>
                confidence{" "}
                <span className="num" style={{ color: "var(--ta-text)" }}>
                  {decision.confidence.toFixed(2)}
                </span>
              </div>
            </div>
          </div>
          <div className="chip-row">
            <RegimeChip regime={decision.regime} label="regime" />
            {pause && (
              <Chip variant="amber" pill>
                paused {pause.until ? `until ${formatTime(pause.until)}` : ""}
              </Chip>
            )}
          </div>
          <p className="decision-why">{decision.reasoning}</p>
          <div className="chip-row">
            {decision.signals.slice(0, 6).map((s) => (
              <SignalChip key={`${s.name}-${s.tf}`} signal={s} />
            ))}
          </div>
          <div className="kv-box">
            <div className="kv">
              <GlossaryTerm term="r/r">Risk / reward</GlossaryTerm>
              <span className="num">
                {decision.plan ? `${formatQty(decision.plan.rr)}R` : "—"}
              </span>
            </div>
            <div className="kv">
              <span>Position size</span>
              <span className="num">
                {decision.plan
                  ? `${formatQty(decision.plan.qty)} · ${formatEur(decision.plan.notional)}`
                  : "no change"}
              </span>
            </div>
            <div className="kv">
              <span>Stop / target</span>
              <span className="num">
                {decision.plan
                  ? `${formatPrice(decision.plan.stop)} / ${formatPrice(decision.plan.target)}`
                  : "—"}
              </span>
            </div>
          </div>
          <div style={{ marginTop: "auto" }} className="pause-row">
            <span className="pause-row__label">
              Pause new entries on this pair
            </span>
            {pair && <PauseSelect pair={pair} pause={pause} />}
          </div>
          <div>
            <Button variant="secondary" onClick={() => onOpen(decision.id)}>
              Open full decision
            </Button>
          </div>
        </>
      ) : (
        <EmptyState
          title="No decisions yet"
          footer={
            pair
              ? `The first ${pair} decision appears after the next cycle.`
              : "The first decision appears after the next cycle."
          }
        />
      )}
    </Card>
  );
}

/* ------------------------------------------------------------- Decisions -- */

const CHANGE_LABEL: Record<CycleSlotChange["kind"], { label: string; cls: string }> = {
  entry: { label: "BUY", cls: "dec-row__label--buy" },
  exit: { label: "SELL", cls: "dec-row__label--sell" },
  stop_move: { label: "STOP UP", cls: "dec-row__label--stop" },
  error: { label: "ERROR", cls: "dec-row__label--error" },
};

const GROUP_CLS: Record<string, string> = {
  BUY: "dec-row__label--buy",
  SELL: "dec-row__label--sell",
  HOLD: "dec-row__label--hold",
  SKIP: "dec-row__label--skip",
  "NO CALL": "dec-row__label--nocall",
  "STOP UP": "dec-row__label--stop",
};

function DecisionsCard({ onOpen }: { onOpen: (id: string) => void }) {
  const cycles = useCyclesRecent(24);
  const items = cycles.data?.decisions ?? [];
  const [open, setOpen] = useState<number | null>(0);
  const [prevDataUpdatedAt, setPrevDataUpdatedAt] = useState(cycles.dataUpdatedAt);
  if (cycles.dataUpdatedAt !== prevDataUpdatedAt) {
    setPrevDataUpdatedAt(cycles.dataUpdatedAt);
    setOpen(0);
  }

  const head = items.slice(0, 3);
  const tail = items.slice(3);
  const tailChanges = tail.flatMap((d) => d.changes);
  const tailMissed = tail.filter((d) => d.missed).length;
  const tailLabel =
    tail.length > 0
      ? `${formatDayShort(tail[0].slot_at)} ${formatTime(tail[0].slot_at)}–${formatTime(tail.at(-1)!.slot_at)} · ${tail.length} cycles · ${tailChanges.length} change${tailChanges.length === 1 ? "" : "s"}${tailMissed ? ` · ${tailMissed} missed` : ""}`
      : null;

  return (
    <Card
      className="row__main"
      label="Decisions · last 24 hours"
      title="Decisions · last 24 hours"
      actions={
        <Link to="/inspector" className="card__link">
          All model calls
        </Link>
      }
    >
      <CardFetch
        query={cycles}
        name="decisions"
        emptyWhen={cycles.data !== undefined && items.length === 0}
        empty={
          <EmptyState
            title="No decisions in the last 24 hours"
            footer="Every cycle's model calls and changes are listed here."
          />
        }
      >
        {head.map((d, i) => (
          <CycleGroupBlock
            key={d.slot_at}
            d={d}
            open={open === i}
            onToggle={() => setOpen(open === i ? null : i)}
            onOpen={onOpen}
          />
        ))}
        {tailLabel && (
          <div>
            <button
              type="button"
              aria-expanded={open === 99}
              className="dec-head"
              onClick={() => setOpen(open === 99 ? null : 99)}
            >
              <span className="dec-head__chev">{open === 99 ? "▾" : "▸"}</span>
              <span className="num dec-head__time">{tailLabel.split(" · ")[0]}</span>
              <span className="dec-head__meta">
                {tailLabel.split(" · ").slice(1).join(" · ")}
              </span>
            </button>
            {tailChanges.map((c, i) => (
              <ChangeRow key={i} c={c} onOpen={onOpen} />
            ))}
          </div>
        )}
        <div className="dec-note">
          A rolling 24 hours, newest first. Changes (entries, exits, stop
          moves) always show; other decisions are grouped by action and
          reason, and folded cycles show their changes only.
        </div>
      </CardFetch>
    </Card>
  );
}

function CycleGroupBlock({
  d,
  open,
  onToggle,
  onOpen,
}: {
  d: CycleDecisions;
  open: boolean;
  onToggle: () => void;
  onOpen: (id: string) => void;
}) {
  const meta = d.missed
    ? "missed"
    : `${d.cycle != null ? `cycle #${d.cycle} · ` : ""}${d.changes.length === 0 ? `no changes · ${d.decision_count} decision${d.decision_count === 1 ? "" : "s"}` : `${d.changes.length} change${d.changes.length === 1 ? "" : "s"} · ${d.decision_count} decision${d.decision_count === 1 ? "" : "s"}`}`;
  return (
    <div>
      <button type="button" aria-expanded={open} className="dec-head" onClick={onToggle}>
        <span className="dec-head__chev">{open ? "▾" : "▸"}</span>
        <span className="num dec-head__time" title={`${formatTimeUtc(d.slot_at)} UTC`}>
          {formatTime(d.slot_at)}
        </span>
        <span className="dec-head__meta">{meta}</span>
      </button>
      {open
        ? d.groups.map((g, i) => (
            <button
              key={i}
              type="button"
              className="dec-row ta-row"
              onClick={() => g.decision_id && onOpen(g.decision_id)}
              disabled={!g.decision_id}
            >
              <span className={`dec-row__label num ${GROUP_CLS[g.label] ?? ""}`}>{g.label}</span>
              <span className="dec-row__body">
                <b>{g.pairs.join(", ")}</b>{" "}
                <span className="dec-row__detail">
                  · {[g.detail, g.reason].filter(Boolean).join(" · ")}
                </span>
              </span>
              <span className="dec-row__count num">{g.count > 1 ? g.count : ""}</span>
            </button>
          ))
        : d.changes.map((c, i) => <ChangeRow key={i} c={c} onOpen={onOpen} />)}
    </div>
  );
}

function ChangeRow({ c, onOpen }: { c: CycleSlotChange; onOpen: (id: string) => void }) {
  const meta = CHANGE_LABEL[c.kind];
  return (
    <button
      type="button"
      className="dec-row ta-row"
      onClick={() => c.decision_id && onOpen(c.decision_id)}
      disabled={!c.decision_id}
    >
      <span className={`dec-row__label num ${meta.cls}`}>{meta.label}</span>
      <span className="dec-row__body">
        <b>{c.pair ?? ""}</b> <span className="dec-row__detail">· {c.detail}</span>
      </span>
      <span className="dec-row__count num" />
    </button>
  );
}

/* -------------------------------------------------------------- Briefing -- */

function BriefingCard({ at }: { at: string }) {
  const briefings = useBriefings("daily", 1);
  const briefing = briefings.data?.items[0];
  return (
    <Card
      className="row__side"
      title={`Morning briefing · ${at}`}
      label="Morning briefing"
      actions={
        <Link to="/reviews" className="card__link">
          All briefings and reviews
        </Link>
      }
    >
      <CardFetch
        query={briefings}
        name="briefing"
        emptyWhen={briefings.data !== undefined && !briefing}
        empty={<EmptyState title="No briefing yet" footer="The first briefing appears at the configured time." />}
      >
        {briefing && <BriefingBody briefing={briefing} />}
      </CardFetch>
    </Card>
  );
}

function BriefingBody({ briefing }: { briefing: BriefingOut }) {
  const f = briefing.facts as Record<string, unknown>;
  const chips: string[] = [];
  if (typeof f["positions_open"] === "number")
    chips.push(`${f["positions_open"]} positions`);
  else if (Array.isArray(f["positions"])) chips.push(`${(f["positions"] as unknown[]).length} positions`);
  if (typeof f["change_24h_eur"] === "string" || typeof f["change_24h_eur"] === "number")
    chips.push(`since yesterday ${formatSignedEur(String(f["change_24h_eur"]))}`);
  if (Array.isArray(f["closest"]))
    chips.push(`closest: ${(f["closest"] as string[]).join(", ")}`);
  const next = new Date(new Date(briefing.at).getTime() + 86_400_000);
  return (
    <>
      <p className="briefing__text">
        <GlossaryText text={briefing.text} />
      </p>
      {chips.length > 0 && (
        <div className="chip-row">
          {chips.map((c) => (
            <Chip key={c} variant="neutral">{c}</Chip>
          ))}
        </div>
      )}
      <div style={{ flex: 1 }} />
      <div className="briefing__foot">
        <span>
          {briefing.source === "template"
            ? `Plain briefing: ${briefing.reason ?? "model unavailable"}`
            : "Numbers checked ✓"}
        </span>
        <span>Next briefing {formatDayShort(next.toISOString())} {formatTime(next.toISOString())}</span>
      </div>
      <div className="card__meta">
        Written by the model from figures computed by the agent at 08:00;
        every number was checked against those figures. Things that changed
        since are in the cards around it.
      </div>
    </>
  );
}

/* --------------------------------------------------------------- Markets -- */

type MarketFilter = "all" | "pass" | "held" | "paused";

function MarketsCard({
  markets,
  heldPairs,
  selected,
  paused,
  onPick,
}: {
  markets: MarketOut[];
  heldPairs: string[];
  selected: string | null;
  paused: Map<string, EntryPause>;
  onPick: (id: string) => void;
}) {
  const [filter, setFilter] = useState<MarketFilter>("all");
  const [query, setQuery] = useState("");
  const held = new Set(heldPairs);

  const counts: Record<MarketFilter, number> = {
    all: markets.length,
    pass: markets.filter((m) => m.rules_state === "passed").length,
    held: markets.filter((m) => held.has(m.market)).length,
    paused: markets.filter((m) => m.paused || m.rules_state === "paused").length,
  };
  const rows = markets
    .filter((m) => {
      if (filter === "pass") return m.rules_state === "passed";
      if (filter === "held") return held.has(m.market);
      if (filter === "paused") return m.paused || m.rules_state === "paused";
      return true;
    })
    .filter((m) => m.market.toLowerCase().includes(query.toLowerCase()));

  return (
    <Card
      label="Markets"
      title={<Link to="/universe">Markets</Link>}
      actions={
        <span className="card__meta" style={{ fontSize: "var(--ta-text-md)" }}>
          {markets.length} pairs · <Link to="/universe">Open Universe</Link>{" "}
          · click a row to chart it
        </span>
      }
    >
      <div className="markets-controls">
        <SegmentedControl
          label="Filter markets"
          value={filter}
          onChange={(v) => setFilter(v as MarketFilter)}
          options={(
            [
              ["all", "All"],
              ["pass", "Pass rules"],
              ["held", "Held"],
              ["paused", "Paused"],
            ] as [MarketFilter, string][]
          ).map(([v, l]) => ({ value: v, label: `${l} ${counts[v]}` }))}
        />
        <input
          type="search"
          aria-label="Find a pair"
          placeholder="Find a pair"
          className="markets-search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      <div className="markets-scroll">
        <div role="list" aria-label="Markets" className="markets-table">
          <div aria-hidden="true" className="markets-row markets-row--head">
            <span>Pair</span>
            <span className="num">Price</span>
            <span className="num">24h</span>
            <span className="gterm" title="4h trend × volatility, and age in 4h bars">Regime</span>
            <span>Rules</span>
            <span>Last action</span>
          </div>
          {rows.map((m) => (
            <div role="listitem" key={m.market}>
              <button
                type="button"
                className="markets-row ta-row"
                aria-pressed={m.market === selected}
                aria-label={`${m.market}, chart it`}
                onClick={() => onPick(m.market)}
              >
                <span className="markets-pair">{m.market}</span>
                <span className="num markets-num">
                  {m.price != null ? formatPrice(m.price) : "—"}
                </span>
                <span className={`num markets-num ${Number(m.chg_24h_pct ?? 0) >= 0 ? "up" : "down"}`}>
                  {m.chg_24h_pct != null ? formatChange(m.chg_24h_pct) : "—"}
                </span>
                <span>
                  {m.regime ? (
                    <RegimeChip regime={m.regime} />
                  ) : (
                    <span className="regime-chip regime-chip--warm">warming up</span>
                  )}
                </span>
                <RulesCell m={m} paused={paused.get(m.market) ?? paused.get("*")} />
                <LastActionCell m={m} />
              </button>
            </div>
          ))}
          {rows.length === 0 && (
            <div className="empty__body" style={{ padding: "10px" }}>
              No pairs match.
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}

function RulesCell({ m, paused }: { m: MarketOut; paused?: EntryPause }) {
  const state = m.rules_state;
  const text =
    state === "passed"
      ? (m.rules_detail ?? "Passed")
      : state === "held"
        ? "Held · reviewed each cycle"
        : state === "failed"
          ? (m.rules_detail ?? "Failed")
          : state === "paused"
            ? `Paused${paused ? " by you" : ""}`
            : state === "warming"
              ? "Warming up"
              : "—";
  return (
    <span className={`markets-rules markets-rules--${state ?? "none"}`}>{text}</span>
  );
}

const ACTION_CLS: Record<string, string> = {
  BUY: "up",
  SELL: "down",
  HOLD: "",
  SKIP: "warn",
};

function LastActionCell({ m }: { m: MarketOut }) {
  if (!m.last_action_action) return <span className="markets-act markets-act--none">—</span>;
  const cls = ACTION_CLS[m.last_action_action] ?? "";
  return (
    <span className={`markets-act num ${cls === "warn" ? "markets-act--warn" : cls === "up" ? "up" : cls === "down" ? "down" : ""}`}>
      {m.last_action_action}
      {m.last_action_at ? ` · ${formatTime(m.last_action_at)}` : ""}
    </span>
  );
}

/* ------------------------------------------------------ PendingApprovals -- */

function PendingApprovals({
  decisions,
  mode,
  vetoWindowSeconds,
  now,
  onOpen,
}: {
  decisions: Decision[];
  mode: "auto" | "supervised" | "manual";
  vetoWindowSeconds: number;
  now: number;
  onOpen: (id: string) => void;
}) {
  const approve = usePendingAction("approve");
  const veto = usePendingAction("veto");
  return (
    <section aria-label="Pending approval" className="approval-grid">
      {decisions.map((decision, index) => {
        const seconds = decision.execute_at
          ? Math.max(0, (new Date(decision.execute_at).getTime() - now) / 1000)
          : null;
        return (
          <Card key={decision.id} className="approval-card">
            <div className="approval-card__head">
              <span className="card__title">
                Waiting for you{decisions.length > 1 ? ` · ${index + 1} of ${decisions.length}` : ""}
              </span>
              <span className="num" style={{ color: "var(--ta-accent)" }}>
                {mode === "supervised" && seconds !== null
                  ? `executes in ${formatCountdown(seconds)}`
                  : "manual approval"}
              </span>
            </div>
            <div className="decision-line">
              <ActionBadge action="BUY" />
              <span className="num">
                {decision.pair} {decision.plan ? `${formatQty(decision.plan.qty)} ≈ ${formatEur(decision.plan.notional)} · R/R ${formatQty(decision.plan.rr)}` : ""}
              </span>
            </div>
            {mode === "supervised" && seconds !== null && (
              <div className="approval-progress">
                <span
                  style={{
                    width: `${Math.max(
                      0,
                      Math.min(100, 100 * (1 - seconds / Math.max(1, vetoWindowSeconds))),
                    )}%`,
                  }}
                />
              </div>
            )}
            <div className="approval-actions">
              <Button variant="danger-outline" disabled={veto.isPending} onClick={() => veto.mutate(decision.id)}>Veto</Button>
              <Button variant="secondary" onClick={() => onOpen(decision.id)}>Details</Button>
              <Button variant="primary" disabled={approve.isPending} onClick={() => approve.mutate(decision.id)}>Approve now</Button>
            </div>
          </Card>
        );
      })}
      <p className="card__meta approval-note">
        Each trade has its own timer. Risk / reward, size and every gate are checked again at fill.
      </p>
    </section>
  );
}
