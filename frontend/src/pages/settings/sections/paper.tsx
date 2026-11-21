import type { RowDef, SectionProps } from "../controls";
import { SectionBody } from "../controls";
import { PaperBlock } from "../blocks";

export const rows: RowDef[] = [
  {
    keys: ["paper.start_balance_eur"],
    label: "Starting balance",
    explanation: "Used at the next reset.",
  },
];

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged}>
      <PaperBlock ctx={ctx} />
    </SectionBody>
  );
}
