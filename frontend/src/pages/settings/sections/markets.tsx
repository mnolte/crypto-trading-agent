import type { ReactNode } from "react";
import type { RowDef, SectionProps } from "../controls";
import { NumInput as Num, SectionBody } from "../controls";
import { MarketsBlock } from "../blocks";

export const rows: RowDef[] = [
  {
    keys: ["active_markets"],
    label: "Active markets",
    explanation:
      "Which Bitvavo EUR markets may be traded. The daily check at 01:00 UTC applies these thresholds; you activate and deactivate markets in the table below. Each active market adds up to 96 model calls a day before the pre-filter.",
    render: () => (
      <span className="settings-row">
        <ButtonLike
          disabled
          title="The Markets screen comes with a later item"
        >
          Open Markets
        </ButtonLike>
        <span className="card__meta">
          The Markets screen comes with a later item; until then the market
          table is shown here, under these thresholds.
        </span>
      </span>
    ),
  },
  { heading: "Eligibility", keys: [], label: "", explanation: "" },
  {
    keys: ["market.min_avg_daily_volume_eur"],
    label: "Average daily volume",
    explanation:
      "30-day average traded value. A market below it can't be activated.",
  },
  {
    keys: ["market.max_avg_spread_pct"],
    label: "Max average spread",
    explanation: "Average bid-ask spread over the last 30 days.",
  },
  {
    keys: ["market.min_daily_candles"],
    label: "Daily history",
    explanation: "Closed daily candles needed before a market is eligible.",
  },
  {
    keys: ["market.eligibility_fail_days"],
    label: "Days failing before it drops",
    explanation:
      "An active market that fails the check this many days in a row is flagged on Needs you; you decide.",
  },
  {
    keys: ["market.stablecoin_bases"],
    label: "Stablecoins",
    explanation:
      "USDC, EURC, USDT, DAI, FDUSD, TUSD, PYUSD and fiat bases are never traded.",
  },
  {
    keys: ["market.history_1h", "market.history_4h", "market.history_1d"],
    label: "History loaded",
    explanation:
      "Candles kept per timeframe, loaded when a market is activated and kept rolling.",
    advanced: true,
    render: (ctx) => (
      <span className="settings-row">
        <Num ctx={ctx} k="market.history_1h" label="1h bars" />
        <Num ctx={ctx} k="market.history_4h" label="4h bars" />
        <Num ctx={ctx} k="market.history_1d" label="1d bars" />
      </span>
    ),
  },
  {
    keys: ["market.stale_grace_s"],
    label: "Freshness grace",
    explanation:
      "A timeframe is stale once its last close is older than the interval plus this grace (1h → 75 min).",
    advanced: true,
  },
  {
    keys: ["market.structure_min_range_bars"],
    label: "Range for structure levels",
    explanation: "Bars a support or resistance level must hold.",
    advanced: true,
  },
  {
    keys: ["market.eligibility_beyond_pct"],
    label: "Eligibility margin",
    explanation:
      "How far past a threshold a market may drift before a failing day counts.",
    advanced: true,
  },
  {
    keys: ["market.eligibility_recover_days"],
    label: "Days to recover",
    explanation:
      "Passing days in a row before a failing market is clear again.",
    advanced: true,
  },
  { heading: "Timeouts", keys: [], label: "", explanation: "", advanced: true },
  {
    keys: ["timeouts.bitvavo_rest_s", "timeouts.stream_read_s"],
    label: "Bitvavo",
    explanation: "REST calls and the price stream.",
    advanced: true,
    render: (ctx) => (
      <span className="settings-row">
        <Num ctx={ctx} k="timeouts.bitvavo_rest_s" label="REST s" />
        <Num ctx={ctx} k="timeouts.stream_read_s" label="stream read s" />
      </span>
    ),
  },
  {
    keys: ["timeouts.openai_s", "timeouts.collector_s"],
    label: "Model and data sources",
    explanation: "One call to OpenAI, one fetch from a context source.",
    advanced: true,
    render: (ctx) => (
      <span className="settings-row">
        <Num ctx={ctx} k="timeouts.openai_s" label="OpenAI s" />
        <Num ctx={ctx} k="timeouts.collector_s" label="sources s" />
      </span>
    ),
  },
  {
    keys: ["timeouts.smtp_s", "timeouts.keyring_s"],
    label: "Email and keyring",
    explanation: "Sending one email, reading one stored key.",
    advanced: true,
    render: (ctx) => (
      <span className="settings-row">
        <Num ctx={ctx} k="timeouts.smtp_s" label="SMTP s" />
        <Num ctx={ctx} k="timeouts.keyring_s" label="keyring s" />
      </span>
    ),
  },
];

function ButtonLike({
  children,
  disabled,
  title,
}: {
  children: ReactNode;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      className="btn btn--secondary btn--sm"
      disabled={disabled}
      title={title}
    >
      {children}
    </button>
  );
}

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged}>
      <MarketsBlock />
    </SectionBody>
  );
}
