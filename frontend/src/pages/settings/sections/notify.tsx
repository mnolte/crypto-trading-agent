import type { Ctx, RowDef, SectionProps } from "../controls";
import { SectionBody } from "../controls";
import { SmtpBlock } from "../blocks";

const EVENT_LABELS: [string, string][] = [
  ["kill_switch", "Kill switch fired"],
  ["daily_loss", "Daily loss limit reached"],
  ["pair_stale", "Pair stale for 2 cycles"],
  ["source_failing", "Data source failing"],
  ["model_failing", "Model failing, budget or cost cap"],
  ["trade", "Trade opened or closed"],
  ["risk_control", "Shock brake or risk throttle"],
  ["review", "Morning briefing and weekly review"],
];

export const rows: RowDef[] = [
  {
    keys: ["notifications.browser_enabled"],
    label: "Browser notifications",
    explanation: "While the app is open in a tab.",
  },
  {
    keys: ["notifications.email_enabled"],
    label: "Email",
    explanation: "Over SMTP, settings below. The password is stored in the keyring.",
  },
  ...EVENT_LABELS.map(
    ([event, label]): RowDef => ({
      keys: [`notifications.events.${event}.browser`, `notifications.events.${event}.email`],
      label,
      explanation: "",
      advanced: true,
      render: (ctx) => <EventCells ctx={ctx} event={event} />,
    }),
  ),
  {
    keys: [],
    label: "Repeats",
    explanation:
      "The same alert is sent at most once per 30 minutes, with a count of repeats.",
    render: () => <span className="set-row__fixed">Once per 30 min · fixed by the spec</span>,
  },
  { heading: "Email (SMTP)", keys: [], label: "", explanation: "" },
  {
    keys: [
      "notifications.smtp.host",
      "notifications.smtp.port",
      "notifications.smtp.tls",
      "notifications.smtp.username",
      "notifications.smtp.sender",
      "notifications.smtp.recipient",
    ],
    label: "Server, account and addresses",
    explanation:
      "TLS is required. The password is stored in the OS keyring and never shown again; it saves with the password field, apart from the unsaved-changes bar.",
    wide: true,
    render: (ctx) => <SmtpBlock ctx={ctx} />,
  },
];

function EventCells({ ctx, event }: { ctx: Ctx; event: string }) {
  return (
    <span className="settings-row">
      <label className="check-label">
        <input
          type="checkbox"
          aria-label={`${event} browser`}
          checked={Boolean(ctx.draft.notifications.events?.[event]?.browser)}
          onChange={(e) =>
            ctx.set("notifications.events", {
              ...ctx.draft.notifications.events,
              [event]: {
                ...ctx.draft.notifications.events?.[event],
                browser: e.target.checked,
              },
            })
          }
        />
        <span>Browser</span>
      </label>
      <label className="check-label">
        <input
          type="checkbox"
          aria-label={`${event} email`}
          checked={Boolean(ctx.draft.notifications.events?.[event]?.email)}
          onChange={(e) =>
            ctx.set("notifications.events", {
              ...ctx.draft.notifications.events,
              [event]: {
                ...ctx.draft.notifications.events?.[event],
                email: e.target.checked,
              },
            })
          }
        />
        <span>Email</span>
      </label>
    </span>
  );
}

export function Section({ ctx, query, onlyChanged }: SectionProps) {
  return (
    <SectionBody ctx={ctx} rows={rows} query={query} onlyChanged={onlyChanged} />
  );
}
