export type Action = "BUY" | "SELL" | "HOLD" | "SKIP";

const CLS: Record<Action, string> = {
  BUY: "action-badge--buy",
  SELL: "action-badge--sell",
  HOLD: "action-badge--hold",
  SKIP: "action-badge--skip",
};

export function ActionBadge({
  action,
  small = false,
}: {
  action: Action;
  small?: boolean;
}) {
  return (
    <span className={`action-badge ${CLS[action]}${small ? " action-badge--sm" : ""}`}>
      {action}
    </span>
  );
}
