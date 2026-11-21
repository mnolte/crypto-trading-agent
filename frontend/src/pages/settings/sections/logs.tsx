import type { RowDef, SectionProps } from "../controls";
import { NumInput as Num, SectionBody } from "../controls";

export const rows: RowDef[] = [
  {
    keys: ["logging.console_level"],
    label: "Console level",
    explanation:
      "Lines below this level go only to logs.db.",
  },
  {
    keys: [
      "logging.keep_days.DEBUG",
      "logging.keep_days.INFO",
      "logging.keep_days.WARNING",
      "logging.keep_days.ERROR",
      "logging.keep_days.CRITICAL",
    ],
    label: "Keep lines for",
    explanation: "Older lines are removed daily at 03:30.",
    render: (ctx) => (
      <span className="settings-row" style={{ flexWrap: "wrap" }}>
        <Num ctx={ctx} k="logging.keep_days.DEBUG" label="debug d" unit="d" />
        <Num ctx={ctx} k="logging.keep_days.INFO" label="info d" unit="d" />
        <Num ctx={ctx} k="logging.keep_days.WARNING" label="warning d" unit="d" />
        <Num ctx={ctx} k="logging.keep_days.ERROR" label="error d" unit="d" />
        <Num ctx={ctx} k="logging.keep_days.CRITICAL" label="critical d" unit="d" />
      </span>
    ),
  },
  {
    keys: ["logging.max_db_mb"],
    label: "Max size",
    explanation: "Above it, the oldest debug lines go first.",
  },
];

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged} />
  );
}
