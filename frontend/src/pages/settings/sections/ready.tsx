import { Link } from "react-router-dom";
import { useReadiness } from "../../../api/hooks";
import type { RowDef, SectionProps } from "../controls";
import { SectionBody } from "../controls";

export const rows: RowDef[] = [
  {
    keys: ["readiness.min_closed_trades"],
    label: "Closed trades",
    explanation: "At least this many.",
  },
  {
    keys: ["readiness.min_days_running"],
    label: "Days running",
    explanation: "At least this many days of paper trading.",
  },
  {
    keys: ["readiness.max_drawdown_pct"],
    label: "Max drawdown",
    explanation: "Never deeper than this during the run.",
  },
  {
    keys: ["readiness.calibration_gap_pts"],
    label: "Calibration gap",
    explanation:
      "Stated confidence against the win rate, at most this far apart.",
  },
  {
    keys: [],
    label: "Checklist",
    explanation:
      "The other criteria (average R, profit factor, beats BTC, no kill, no unreviewed errors) are fixed by the spec. Lowering a threshold is looser and shows on the Readiness screen with its date.",
    render: () => <ChecklistLink />,
  },
];

function ChecklistLink() {
  const { data: readiness } = useReadiness();
  return readiness ? (
    <Link to="/readiness" className="btn btn--secondary btn--sm">
      Open Readiness · {readiness.met} of {readiness.total} met
    </Link>
  ) : (
    <Link to="/readiness" className="btn btn--secondary btn--sm">
      Open Readiness
    </Link>
  );
}

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged} />
  );
}
