import type { RowDef, SectionProps } from "../controls";
import { SectionBody } from "../controls";

export const rows: RowDef[] = [
  {
    keys: ["execution_mode"],
    label: "Mode",
    explanation:
      "Automatic places the paper order straight away. Supervised waits the veto window, Manual waits for your approval until the pair's next cycle. Exits never wait.",
  },
  {
    keys: ["veto_window_s"],
    label: "Veto window",
    explanation:
      "Supervised only: how long an entry waits for your veto on the Dashboard.",
  },
  {
    keys: [],
    label: "Cycle",
    explanation: "Every 15 minutes; the 1h timeframe with 4h and daily context.",
    render: () => (
      <span className="set-row__fixed">Every 15 min · fixed by the spec</span>
    ),
  },
  {
    keys: ["cycle_offset_s"],
    label: "Cycle start",
    explanation:
      "Seconds after the quarter hour, so the 1h candle has closed at Bitvavo.",
  },
  {
    keys: ["risk.pending_entry_drift_pct"],
    label: "Max price drift while waiting",
    explanation:
      "A waiting entry expires when price moves this far from its planned entry.",
  },
  {
    keys: [],
    label: "Live trading",
    explanation:
      "Real orders are not built. The Readiness screen shows when paper results would allow it.",
    render: () => <span className="set-row__fixed">Paper only · locked</span>,
  },
];

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged} />
  );
}
