import { useSnapshot } from "../../../api/hooks";
import { formatEur } from "../../../lib/format";
import type { Ctx, RowDef, SectionProps } from "../controls";
import { NumInput as Num, SectionBody } from "../controls";

export const rows: RowDef[] = [
  { heading: "Decisions", keys: [], label: "", explanation: "" },
  {
    keys: ["model.decider"],
    label: "Decider",
    explanation:
      "Rules decides on its own without a key or when the model is off.",
  },
  {
    keys: ["model.model_id"],
    label: "Model",
    explanation:
      "Changing the live model asks once more. Try a new one as a shadow first.",
  },
  {
    keys: ["model.prompt_version"],
    label: "Prompt version",
    explanation: "A version in use is never edited; a new prompt is a new file.",
  },
  {
    keys: ["model.rules_prefilter"],
    label: "Rules pre-filter",
    explanation:
      "Only call the model when Rules sees a possible setup or a position is open.",
  },
  {
    keys: ["model.daily_call_budget"],
    label: "Daily call budget",
    explanation:
      "Calls a day for live decisions; at the budget, decisions use Rules until 00:00.",
  },
  {
    keys: ["model.monthly_cost_cap_eur"],
    label: "Monthly cost cap",
    explanation:
      "Decisions, shadows, Ask, briefings and replays together. At 80% an alert; at 100% Ask and replays pause and decisions use Rules.",
    render: (ctx) => <CostCap ctx={ctx} />,
  },
  { heading: "Briefing and review", keys: [], label: "", explanation: "" },
  {
    keys: ["learning.briefing_time"],
    label: "Morning briefing",
    explanation: "A short note on the night, written from numbers computed in code.",
  },
  {
    keys: ["learning.weekly_review"],
    label: "Weekly review",
    explanation: "The week's results against the week before and BTC.",
  },
  { heading: "Ask the agent and Insights", keys: [], label: "", explanation: "" },
  {
    keys: ["learning.ask_daily_limit"],
    label: "Questions per day",
    explanation: "Ask stops for the day after this many.",
  },
  {
    keys: ["learning.insights_min_decisions"],
    label: "Decisions for a finding",
    explanation:
      "Insights shows a pattern only with at least this many decisions behind it.",
  },
];

function CostCap({ ctx }: { ctx: Ctx }) {
  const { data: snapshot } = useSnapshot();
  return (
    <span className="settings-row" style={{ flexDirection: "column", alignItems: "flex-end", gap: 4 }}>
      <Num ctx={ctx} k="model.monthly_cost_cap_eur" label="Monthly cost cap" unit="€" />
      {snapshot && (
        <span className="card__meta">
          this month {formatEur(snapshot.usage.cost_month_eur)} of{" "}
          {formatEur(snapshot.usage.cap_eur)}
        </span>
      )}
    </span>
  );
}

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <>
      <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged} />
      <p className="card__meta" style={{ margin: 0 }}>
        Briefings, reviews and answers are written by the model from numbers
        computed in code. A check rejects any text with a number that isn&apos;t
        in the facts.
      </p>
    </>
  );
}
