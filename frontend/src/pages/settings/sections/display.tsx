import type { RowDef, SectionProps } from "../controls";
import { SectionBody } from "../controls";

export const rows: RowDef[] = [
  {
    keys: ["learning.plain_language"],
    label: "Plain-language mode",
    explanation:
      "Explains jargon in tooltips and writes summaries for a non-trader.",
  },
  {
    keys: ["learning.quiet_line"],
    label: "“Why so quiet?” line",
    explanation:
      "On the Dashboard: what the agent did this cycle and why it didn't trade.",
  },
  {
    keys: ["ui.stop_near_atr"],
    label: "Near-stop warning",
    explanation: "A position's distance to its stop turns amber below this.",
  },
];

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged} />
  );
}
