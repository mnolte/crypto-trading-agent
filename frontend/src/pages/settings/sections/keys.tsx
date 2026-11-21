import type { RowDef, SectionProps } from "../controls";
import { SectionBody } from "../controls";
import { SourcesBlock } from "../blocks";

export const rows: RowDef[] = [
  {
    keys: ["data_source"],
    label: "Price data",
    explanation:
      "Simulated replays stored candles for offline demos and shows a SIMULATED badge.",
  },
  {
    keys: ["disabled_sources"],
    label: "Data sources",
    explanation:
      "Keys are stored in the OS keyring and saved straight away, one at a time, apart from the unsaved-changes bar. Only the last 4 characters are shown. A source whose required key is missing stays off with a clear message; the agent keeps running without it.",
    wide: true,
    render: () => <SourcesBlock />,
  },
];

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged} />
  );
}
