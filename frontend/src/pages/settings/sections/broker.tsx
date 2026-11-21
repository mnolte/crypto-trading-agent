import type { RowDef, SectionProps } from "../controls";
import { SectionBody } from "../controls";

export const rows: RowDef[] = [
  {
    keys: ["paper.taker_fee_pct"],
    label: "Taker fee",
    explanation: "Market orders: entries, stops and forced exits.",
  },
  {
    keys: ["paper.maker_fee_pct"],
    label: "Maker fee",
    explanation: "Limit orders that fill at their price.",
  },
  {
    keys: ["paper.slippage_pct"],
    label: "Slippage on market orders",
    explanation: "Fill = reference price ± this.",
  },
  {
    keys: ["paper.entry_orders"],
    label: "Entry orders",
    explanation:
      "Limit (post-only) entries pay the maker fee and may not fill; an unfilled entry is cancelled at the next cycle. Tested as Shadow B.",
  },
  {
    keys: [],
    label: "Stops and forced exits",
    explanation: "Always market orders, whatever the entry order type.",
    render: () => <span className="set-row__fixed">Always market · fixed by the spec</span>,
  },
];

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged} />
  );
}
