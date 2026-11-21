import type { Ctx, RowDef, SectionProps } from "../controls";
import { NumInput as Num, SectionBody } from "../controls";
import { formatEur } from "../../../lib/format";
import { getPath } from "../model";

export const rows: RowDef[] = [
  { heading: "Per trade", keys: [], label: "", explanation: "" },
  {
    keys: ["risk.risk_per_trade_pct"],
    label: "Risk per trade",
    explanation:
      "What you lose if the stop is hit. Quantity = this budget ÷ (entry − stop).",
  },
  {
    keys: ["risk.min_rr"],
    label: "Min reward to risk",
    explanation: "Distance to the target ÷ distance to the stop, after fees.",
  },
  {
    keys: ["risk.max_cost_pct_of_risk"],
    label: "Max costs",
    explanation:
      "Fees and spread for entry and exit, as a share of the risk. Small trades fail this first.",
  },
  {
    keys: ["risk.stop_min_atr", "risk.stop_max_atr"],
    label: "Stop distance",
    explanation:
      "A stop closer or further than this, in ATR(1h), ends as HOLD.",
    render: (ctx) => (
      <span className="settings-row">
        <Num ctx={ctx} k="risk.stop_min_atr" label="from ATR" />
        <Num ctx={ctx} k="risk.stop_max_atr" label="to ATR" />
      </span>
    ),
  },
  {
    keys: ["risk.stop_buffer_atr"],
    label: "Stop buffer",
    explanation: "The stop sits this far below the support level it is based on.",
  },
  {
    keys: ["risk.min_trade_eur"],
    label: "Min trade size",
    explanation: "Smaller orders are skipped.",
  },
  { heading: "Portfolio", keys: [], label: "", explanation: "" },
  {
    keys: ["risk.total_open_risk_pct"],
    label: "Total open risk",
    explanation:
      "Entry-to-stop risk of all positions and waiting entries together.",
  },
  {
    keys: ["risk.max_exposure_pct"],
    label: "Max exposure",
    explanation:
      "Value of all positions together. Cash only: margin is never used.",
  },
  {
    keys: ["risk.correlated_max_positions", "risk.correlated_threshold"],
    label: "Correlated positions",
    explanation:
      "At most this many positions whose 30-day correlation with each other is above the threshold.",
    render: (ctx) => (
      <span className="settings-row">
        <Num ctx={ctx} k="risk.correlated_max_positions" label="at most positions" />
        <span className="card__meta">above</span>
        <Num ctx={ctx} k="risk.correlated_threshold" label="correlation" />
      </span>
    ),
  },
  {
    keys: ["risk.cooldown_after_stopout_hours"],
    label: "Cooldown after a stop-out",
    explanation: "No new entry on that pair for this long.",
  },
  { heading: "Brakes", keys: [], label: "", explanation: "" },
  {
    keys: ["risk.daily_loss_limit_pct"],
    label: "Daily loss limit",
    explanation:
      "No new entries for the rest of the day (until 00:00 Amsterdam) once equity is this far below the day's start.",
  },
  {
    keys: ["risk.throttle_consecutive_losses", "risk.throttle_drawdown_pct"],
    label: "Risk throttle",
    explanation:
      "Risk per trade drops to the throttled value after this many losses in a row, or this far below the peak; each lifts on its own.",
    render: (ctx) => (
      <span className="settings-row">
        <Num ctx={ctx} k="risk.throttle_consecutive_losses" label="losses in a row" />
        <span className="card__meta">or drawdown</span>
        <Num ctx={ctx} k="risk.throttle_drawdown_pct" label="drawdown %" unit="%" />
      </span>
    ),
  },
  {
    keys: ["risk.throttled_risk_per_trade_pct"],
    label: "Throttled risk per trade",
    explanation: "Risk per trade while the throttle is on.",
  },
  {
    keys: ["risk.throttle_lift_drawdown_pct"],
    label: "Throttle lifts at",
    explanation:
      "The drawdown trigger lifts once drawdown from the same peak is this or less.",
  },
  {
    keys: ["risk.shock_brake_btc_move_pct", "risk.shock_brake_hours"],
    label: "Shock brake",
    explanation:
      "No new entries on any pair when BTC-EUR moves this much within 60 minutes; exits keep working.",
    render: (ctx) => (
      <span className="settings-row">
        <Num ctx={ctx} k="risk.shock_brake_btc_move_pct" label="BTC move %" unit="%" />
        <Num ctx={ctx} k="risk.shock_brake_hours" label="pause h" unit="h" />
      </span>
    ),
  },
  {
    keys: ["risk.kill_drawdown_pct"],
    label: "Drawdown kill",
    explanation: "Flatten everything and stop when equity is this far below its peak.",
  },
];

/** "What these limits mean today" — the backend-computed summary. */
export function RiskSummary({ ctx }: { ctx: Ctx }) {
  const s = ctx.preview?.summary;
  if (!s) return null;
  const eq = ["risk.max_cost_pct_of_risk"].map((k) => ctx.preview?.equivalents?.[k]).find(Boolean);
  const riskPct = String(getPath(ctx.draft, "risk.risk_per_trade_pct") ?? "");
  const openPct = String(getPath(ctx.draft, "risk.total_open_risk_pct") ?? "");
  const dailyPct = String(getPath(ctx.draft, "risk.daily_loss_limit_pct") ?? "");
  const killPct = String(getPath(ctx.draft, "risk.kill_drawdown_pct") ?? "");
  return (
    <div className="set-summary" role="note">
      <p>
        What these limits mean today at equity {formatEur(s.equity)}
        {ctx.preview ? " · with your unsaved changes" : ""}.
      </p>
      <p>
        A trade that hits its stop loses about {formatEur(s.loss_at_stop_eur)} (
        {formatEur(s.throttled_loss_eur)} while the risk throttle is on).
        {eq ? ` Fees and spread may take at most ${eq} of that.` : ""}
      </p>
      <p>
        {s.positions_at_full_risk} position
        {s.positions_at_full_risk === 1 ? "" : "s"} at full risk use{" "}
        {riskPct}% of the {openPct}% open-risk budget; the next is sized down
        to {s.next_position_pct}%, so {s.positions_at_full_risk} is the
        practical limit.
      </p>
      <p>
        New entries stop for the rest of the day at −{formatEur(s.daily_stop_eur)} (
        {dailyPct}% of the day&apos;s start at {formatEur(s.day_start_equity)});
        the kill switch turns on at {formatEur(s.kill_level_eur)}, {killPct}%
        below the peak of {formatEur(s.peak_equity)}.
      </p>
    </div>
  );
}

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged}>
      <RiskSummary ctx={ctx} />
    </SectionBody>
  );
}
