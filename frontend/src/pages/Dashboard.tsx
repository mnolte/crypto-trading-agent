import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { UTCTimestamp } from "lightweight-charts";
import type {
  ActivityItem,
  BriefingOut,
  Decision,
  EntryPause,
  Position,
  RiskBudget,
  Snapshot,
  Tf,
} from "../api/client";
import {
  useBriefings,
  useCandles,
  useDecisions,
  useNow,
  usePendingAction,
  useSettings,
  useSnapshot,
  useSources,
} from "../api/hooks";
import { ActionBadge } from "../components/ActionBadge";
import { AppShell } from "../components/AppShell";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { PriceChart, type PriceLine, type PriceMarker, type Candle } from "../components/Charts";
import { Chip } from "../components/Chip";
import { DataTable } from "../components/DataTable";
import { DecisionDrawer } from "../components/DecisionDrawer";
import { EmptyState } from "../components/EmptyState";
import { GlossaryText } from "../components/Glossary";
import { GlossaryTerm } from "../components/GlossaryTerm";
import { KpiTile } from "../components/KpiTile";
import { Meter } from "../components/Meter";
import { PairTabs, type PairTabItem } from "../components/PairTabs";
import { PauseSelect } from "../components/PauseSelect";
import { RegimeChip } from "../components/RegimeChip";
import { SegmentedControl } from "../components/SegmentedControl";
import { SignalChip } from "../components/SignalChip";
import {
  formatAge,
  formatChange,
  formatCountdown,
  formatEur,
  formatPct,
  formatPrice,
  formatQty,
  formatSignedEur,
  formatTime,
} from "../lib/format";

const FIRST_RUN_KEY = "ta.firstRun.dismissed";
const TFS: Tf[] = ["1h", "4h", "1d"];

/** Tab badge text for an active pause: "18h" for timed, "" for indefinite. */
function pauseBadge(
  pause: { until: string | null } | undefined,
  now: number,
): string | undefined {
  if (pause === undefined) return undefined;
  if (pause.until === null) return "";
  const left = (new Date(pause.until).getTime() - now) / 1000;
  return left > 0 ? formatAge(left) : "";
}

export function Dashboard() {
  const { data: snapshot } = useSnapshot();
  const { data: settings } = useSettings();
  const { data: sources } = useSources();
  const now = useNow();
  const [searchParams] = useSearchParams();

  const [pair, setPair] = useState<string | null>(null);
  const [tf, setTf] = useState<Tf>("1h");
  const [decisionId, setDecisionId] = useState<string | null>(null);
  const [firstRunDismissed, setFirstRunDismissed] = useState(
    () => localStorage.getItem(FIRST_RUN_KEY) === "1",
  );

  const markets = settings?.active_markets ?? [];
  const selected = pair ?? markets[0] ?? null;
  const { data: feed } = useCandles(selected, tf);
  const { data: decisions } = useDecisions(selected ?? undefined, 1);
  const latest = decisions?.items[0] ?? null;
  const { data: briefings } = useBriefings("daily", 1);

  const price = selected ? snapshot?.prices[selected] : undefined;
  const openai = sources?.find((s) => s.id === "openai");
  const showFirstRun = openai !== undefined && !openai.key_set && !firstRunDismissed;

  const paused = new Map((snapshot?.paused ?? []).map((p) => [p.pair, p]));
  const pairTabs: PairTabItem[] = markets.map((m) => ({
    id: m,
    chg24h: Number(snapshot?.prices[m]?.chg_24h_pct ?? 0),
    stale: snapshot?.prices[m]?.stale ?? false,
    paused: pauseBadge(paused.get(m) ?? paused.get("*"), now),
  }));

  const candles: Candle[] = (feed?.candles ?? []).map((c) => ({
    time: (new Date(c.time).getTime() / 1000) as UTCTimestamp,
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
    volume: c.volume,
  }));
  const markers: PriceMarker[] = (feed?.markers ?? []).map((m) => ({
    time: (new Date(m.time).getTime() / 1000) as UTCTimestamp,
    kind: m.kind,
    text: m.kind === "buy" ? "B" : m.kind === "sell" ? "S" : "D",
  }));
  const lines: PriceLine[] = feed?.position
    ? [
        { price: Number(feed.position.entry), color: "accent", title: "entry" },
        { price: Number(feed.position.stop), color: "down", title: "stop" },
        { price: Number(feed.position.target), color: "up", title: "target" },
      ]
    : [];

  return (
    <AppShell title="Dashboard">
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

      {(settings?.learning.quiet_line ?? true) && snapshot?.quiet && (
        <div role="status" className="quiet-line">
          <span className="quiet-line__chip">This cycle</span>
          <span className="quiet-line__text">
            <GlossaryText text={snapshot.quiet} />
          </span>
          <Link to="/insights" className="quiet-line__link">
            Why?
          </Link>
        </div>
      )}

      <KpiStrip snapshot={snapshot} now={now} />

      <div className="row">
        <BriefingCard
          briefing={briefings?.items[0]}
          at={settings?.learning.briefing_time ?? "08:00"}
        />
        <RiskBudgetCard budget={snapshot?.risk_budget} />
      </div>

      <div className="row">
        <Card className="row__main" label="Market chart">
          <div className="chart-head">
            <PairTabs pairs={pairTabs} value={selected ?? ""} onChange={setPair} />
            <SegmentedControl
              flat
              label="Timeframe"
              value={tf}
              onChange={(v) => setTf(v as Tf)}
              options={TFS.map((t) => ({ value: t, label: t }))}
            />
          </div>
          <div className="chart-price">
            <span className="chart-price__value num">
              {price ? formatPrice(price.last) : "—"}
            </span>
            <span
              className={`num ${
                Number(price?.chg_24h_pct ?? 0) >= 0 ? "up" : "down"
              }`}
            >
              {price ? `${formatChange(price.chg_24h_pct)} 24h` : ""}
            </span>
          </div>
          {candles.length > 0 ? (
            <PriceChart candles={candles} markers={markers} lines={lines} />
          ) : (
            <EmptyState
              title={selected ? "Loading candles…" : "No active market"}
              footer={
                selected
                  ? undefined
                  : "Turn a market on in Settings to see its chart."
              }
            />
          )}
          <div className="chart-legend">
            <span className="chart-legend__item">
              <span className="chart-marker chart-marker--buy">B</span>Agent buy
            </span>
            <span className="chart-legend__item">
              <span className="chart-marker chart-marker--sell">S</span>Agent
              sell
            </span>
            <span className="chart-legend__item">
              <span className="chart-marker chart-marker--decision">D</span>
              Decision
            </span>
          </div>
        </Card>

        <LatestDecision
          decision={latest}
          pair={selected}
          pause={selected ? (paused.get(selected) ?? paused.get("*")) : undefined}
          onOpen={(id) => setDecisionId(id)}
        />
      </div>

      {(snapshot?.pending.length ?? 0) > 0 && (
        <PendingApprovals
          decisions={snapshot?.pending ?? []}
          mode={settings?.execution_mode ?? "auto"}
          vetoWindowSeconds={settings?.veto_window_s ?? 60}
          now={now}
          onOpen={setDecisionId}
        />
      )}

      <div className="row">
        <PositionsCard positions={snapshot?.positions ?? []} />
        <ActivityCard
          activity={snapshot?.activity ?? []}
          errorsOnly={searchParams.get("activity") === "errors"}
        />
      </div>

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

function KpiStrip({ snapshot, now }: { snapshot?: Snapshot; now: number }) {
  const countdown = snapshot?.next_run_at
    ? (new Date(snapshot.next_run_at).getTime() - now) / 1000
    : null;
  const nextText = snapshot?.cycle_in_progress
    ? "running"
    : countdown === null
      ? "paused"
      : countdown > 0
        ? formatCountdown(countdown)
        : "due";

  const pnl = Number(snapshot?.today_pnl ?? 0);
  const ret = snapshot?.return_since_start_pct ?? 0;
  const drawdown = snapshot?.drawdown_pct ?? 0;
  const killPct = Number(snapshot?.risk_budget.kill_pct ?? 8);

  return (
    <section aria-label="Account summary" className="kpi-strip">
      <KpiTile
        label="Equity"
        value={snapshot ? formatEur(snapshot.equity) : "—"}
        sub={`${formatChange(ret)} since start`}
        subTone={ret >= 0 ? "up" : "down"}
      />
      <KpiTile
        label="Today P&L"
        value={
          <span style={{ color: pnl >= 0 ? "var(--up)" : "var(--down-text)" }}>
            {snapshot ? formatSignedEur(snapshot.today_pnl) : "—"}
          </span>
        }
        sub={
          snapshot
            ? `${snapshot.today_trades} trades · ${snapshot.today_won} won`
            : undefined
        }
      />
      <KpiTile
        label="Exposure"
        glossaryTerm="exposure"
        value={snapshot ? formatPct(snapshot.exposure_pct) : "—"}
        meter={{ pct: snapshot?.exposure_pct ?? 0, color: "accent" }}
        sub="spot, no leverage"
      />
      <KpiTile
        label="Drawdown"
        value={
          <span style={{ color: drawdown > 0 ? "var(--down-text)" : undefined }}>
            {snapshot ? `−${drawdown.toFixed(1)}%` : "—"}
          </span>
        }
        meter={{
          pct: killPct > 0 ? (drawdown / killPct) * 100 : 0,
          color: "down",
        }}
        sub={`kill at −${formatPct(killPct)}`}
      />
      <KpiTile
        label="Next cycle"
        value={nextText}
        sub={
          snapshot ? `every 15 min · cycle #${snapshot.cycle}` : undefined
        }
      />
      <KpiTile
        label="Model today"
        value={snapshot ? `${snapshot.usage.calls_today} calls` : "—"}
        sub={
          snapshot
            ? `of ${snapshot.usage.daily_budget} · ${formatEur(snapshot.usage.cost_today_eur)} today`
            : undefined
        }
      />
    </section>
  );
}

function BriefingCard({
  briefing,
  at,
}: {
  briefing: BriefingOut | undefined;
  at: string;
}) {
  return (
    <Card
      className="row__main"
      title={`Morning briefing · ${at}`}
      label="Morning briefing"
      actions={
        <Link to="/reviews" className="card__link">
          All briefings and reviews
        </Link>
      }
    >
      {briefing ? (
        <>
          <p className="briefing__text">
            <GlossaryText text={briefing.text} />
          </p>
          <div className="card__meta">
            {briefing.source === "template"
              ? `Plain briefing: ${briefing.reason}.`
              : "Written by the model from figures computed by the agent; every number was checked against those figures."}
          </div>
        </>
      ) : (
        <EmptyState
          title="No briefing yet"
          footer="The first briefing appears at the configured time."
        />
      )}
    </Card>
  );
}

function RiskBudgetCard({ budget }: { budget: RiskBudget | undefined }) {
  if (!budget) return null;
  const openRisk = Number(budget.open_risk_pct);
  const openCap = Number(budget.open_risk_cap_pct);
  const todayLoss = Number(budget.today_loss_pct);
  const dailyLimit = Number(budget.daily_limit_pct);
  const drawdown = Number(budget.drawdown_pct);
  const kill = Number(budget.kill_pct);
  const room =
    budget.full_trades_left <= 0
      ? "No room for another trade."
      : budget.room_limited_by === "cash"
        ? `Room for ${budget.full_trades_left} more trade${budget.full_trades_left === 1 ? "" : "s"}, limited by available cash.`
        : `Room for ${budget.full_trades_left} more trade${budget.full_trades_left === 1 ? "" : "s"}.`;
  return (
    <Card className="row__side" title="Risk budget" label="Risk budget">
      <div className="risk-line">
        <div className="risk-line__head">
          <GlossaryTerm term="open risk">Open risk</GlossaryTerm>
          <span className="num">
            {openRisk.toFixed(1)}% of {formatPct(openCap)} cap
          </span>
        </div>
        <Meter pct={openCap > 0 ? (openRisk / openCap) * 100 : 0} />
      </div>
      <div className="risk-line">
        <div className="risk-line__head">
          <span>Today's loss</span>
          <span className="num">
            {todayLoss.toFixed(1)}% of {formatPct(dailyLimit)} limit
          </span>
        </div>
        <Meter pct={dailyLimit > 0 ? (todayLoss / dailyLimit) * 100 : 0} />
      </div>
      <div className="risk-line">
        <div className="risk-line__head">
          <GlossaryTerm term="drawdown">Drawdown</GlossaryTerm>
          <span className="num">
            −{drawdown.toFixed(1)}% of −{formatPct(kill)} kill
          </span>
        </div>
        <Meter pct={kill > 0 ? (drawdown / kill) * 100 : 0} color="down" />
      </div>
      <div className="kv-box">
        <span>{room}</span>
        <div className="kv">
          <GlossaryTerm term="correlated">Correlated</GlossaryTerm>
          <span className="num">
            {budget.correlated_detail ?? `${budget.correlated_slots_left} slots left`}
          </span>
        </div>
        <div className="kv">
          <span>Risk per trade</span>
          <span className="num">
            {budget.risk_pct_now}% · {budget.throttle ? "throttle on" : "throttle off"}
          </span>
        </div>
        <div className="kv">
          <GlossaryTerm term="shock brake">Shock brake</GlossaryTerm>
          <span className="num">
            {budget.shock_brake_until
              ? `on until ${formatTime(budget.shock_brake_until)}`
              : "off"}
          </span>
        </div>
      </div>
    </Card>
  );
}

function LatestDecision({
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
          <span className="num" style={{ fontSize: 12, color: "var(--text-muted)" }}>
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
              <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
                confidence{" "}
                <span className="num" style={{ color: "var(--text)" }}>
                  {decision.confidence.toFixed(2)}
                </span>
              </div>
            </div>
          </div>
          <div>
            <RegimeChip regime={decision.regime} label="regime" />
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
              Pause new entries on this coin
            </span>
            {pair && <PauseSelect pair={pair} pause={pause} />}
          </div>
          <div>
            <Button variant="secondary" onClick={() => onOpen(decision.id)}>
              Open full reasoning
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
              <span className="num" style={{ color: "var(--accent)" }}>
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

function PositionsCard({ positions }: { positions: Position[] }) {
  const now = useNow(30_000);
  return (
    <Card className="row__main" title="Open positions" label="Open positions">
      {positions.length > 0 ? (
        <DataTable
          minWidth={520}
          columns={[
            { key: "pair", label: "Pair" },
            { key: "side", label: "Side" },
            { key: "size", label: "Size", numeric: true },
            { key: "entry", label: "Entry", numeric: true },
            { key: "stop", label: "Stop", numeric: true },
            { key: "age", label: "Age", numeric: true },
            { key: "pnl", label: "Unrealised", numeric: true },
          ]}
        >
          {positions.map((p) => {
            const pnl = Number(p.unrealised);
            return (
              <tr key={p.pair}>
                <td className="text--strong">{p.pair}</td>
                <td style={{ color: "var(--up)" }}>Long</td>
                <td className="num">{formatQty(p.qty)}</td>
                <td className="num">{formatPrice(p.entry)}</td>
                <td className="num">{formatPrice(p.stop)}</td>
                <td className="num">
                  {formatAge((now - new Date(p.opened_at).getTime()) / 1000)}
                </td>
                <td
                  className="num"
                  style={{ color: pnl >= 0 ? "var(--up)" : "var(--down-text)" }}
                >
                  {formatSignedEur(p.unrealised)}
                </td>
              </tr>
            );
          })}
        </DataTable>
      ) : (
        <EmptyState
          title="No open positions"
          footer="New BUYs appear here after a decision executes."
        />
      )}
    </Card>
  );
}

const ACTIVITY_TONE: Record<string, "up" | "down" | "amber" | "neutral"> = {
  buy: "up",
  sell: "down",
  skip: "amber",
  hold: "neutral",
  error: "down",
  kill: "down",
};

function ActivityCard({
  activity,
  errorsOnly,
}: {
  activity: ActivityItem[];
  errorsOnly: boolean;
}) {
  const items = errorsOnly ? activity.filter((item) => item.kind === "error") : activity;
  return (
    <Card
      className="row__side"
      title="Activity"
      label="Activity"
      actions={<span className="card__meta">{errorsOnly ? "errors · 24h" : "all pairs"}</span>}
    >
      <span id="activity" style={{ position: "absolute", marginTop: -80 }} />
      {items.length > 0 ? (
        <ol className="activity">
          {items.slice(0, 20).map((item, i) => (
            <li key={`${item.at}-${i}`} className="activity__row">
              <span className="activity__time num">{formatTime(item.at)}</span>
              <Chip variant={ACTIVITY_TONE[item.kind] ?? "neutral"}>
                {item.kind.toUpperCase()}
              </Chip>
              <span className="activity__text">{item.text}</span>
            </li>
          ))}
        </ol>
      ) : (
        <EmptyState
          title="Nothing yet"
          footer="Decisions, fills and errors are listed here."
        />
      )}
    </Card>
  );
}
