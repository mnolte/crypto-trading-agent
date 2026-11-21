import type { RowDef, SectionProps } from "../controls";
import { NumInput as Num, SectionBody } from "../controls";

export const rows: RowDef[] = [
  {
    keys: ["market.adx_threshold"],
    label: "Trend strength (ADX)",
    explanation: "Shared by the regime, Rules condition C6 and Insights.",
  },
  { heading: "Pre-filter and Rules BUY", keys: [], label: "", explanation: "" },
  {
    keys: ["rules.prefilter_min_conds"],
    label: "Pre-filter",
    explanation:
      "The model is only asked when C1 holds and at least this many of C2–C6, or a position is open.",
  },
  {
    keys: ["rules.buy_min_conds"],
    label: "Rules BUY",
    explanation: "Conditions a Rules BUY needs, of C2–C6.",
  },
  {
    keys: ["rules.buy_max_atr_above_ema20"],
    label: "Max distance above EMA 20",
    explanation: "A Rules BUY is skipped when price is further above the 1h EMA 20.",
  },
  { heading: "Rules stop and target", keys: [], label: "", explanation: "" },
  {
    keys: ["rules.stop_lookback_bars", "rules.stop_atr_pad", "rules.stop_max_atr"],
    label: "Stop",
    explanation:
      "Below the lowest low of the last bars, minus a pad; never further than the max.",
    render: (ctx) => (
      <span className="settings-row">
        <Num ctx={ctx} k="rules.stop_lookback_bars" label="lookback bars" />
        <Num ctx={ctx} k="rules.stop_atr_pad" label="pad ATR" />
        <Num ctx={ctx} k="rules.stop_max_atr" label="max ATR" />
      </span>
    ),
  },
  {
    keys: ["rules.target_max_atr", "rules.target_rr"],
    label: "Target",
    explanation:
      "The nearest resistance, capped; at least this reward to risk.",
    render: (ctx) => (
      <span className="settings-row">
        <Num ctx={ctx} k="rules.target_max_atr" label="max ATR" />
        <Num ctx={ctx} k="rules.target_rr" label="R/R" unit="×" />
      </span>
    ),
  },
];

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged} />
  );
}
