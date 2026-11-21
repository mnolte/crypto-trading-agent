import type { RowDef, SectionProps } from "../controls";
import { SectionBody } from "../controls";

export const rows: RowDef[] = [
  {
    keys: ["risk.time_stop_days"],
    label: "Time stop",
    explanation:
      "Close at market when a position is still open this long after entry.",
  },
  {
    keys: ["risk.trailing_stop_enabled"],
    label: "Trailing stop",
    explanation:
      "From +2R the stop follows the highest 1h close minus 2 × ATR(1h) and never moves down. Try it in What-if or as a shadow first.",
  },
  {
    keys: [],
    label: "Breakeven",
    explanation: "At +1R the stop moves to the entry plus both fees.",
    render: () => <span className="set-row__fixed">Always on · fixed by the spec</span>,
  },
  {
    keys: [],
    label: "Stops and forced exits",
    explanation:
      "Stops, the time stop, the kill and Stop & flatten are market orders.",
    render: () => <span className="set-row__fixed">Always market · fixed by the spec</span>,
  },
];

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged} />
  );
}
