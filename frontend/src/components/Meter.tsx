export type MeterColor = "accent" | "up" | "down" | "muted" | "info";

const SEGMENT_VAR: Record<MeterColor, string> = {
  accent: "var(--ta-accent)",
  up: "var(--ta-positive)",
  down: "var(--ta-negative)",
  muted: "var(--ta-text-faint)",
  info: "var(--ta-info)",
};

/** Progress bar for value-vs-limit displays (exposure, drawdown, risk). */
export function Meter({
  pct,
  color = "accent",
  large = false,
  segments,
}: {
  /** 0..100 */
  pct?: number;
  color?: MeterColor;
  large?: boolean;
  /** Stacked segments, e.g. the monthly cost split. */
  segments?: { pct: number; color: MeterColor }[];
}) {
  const cls = `meter${large ? " meter--lg" : ""}${segments ? " meter--stacked" : ` meter--${color}`}`;
  return (
    <div className={cls} aria-hidden="true">
      {segments ? (
        segments.map((s, i) => (
          <span
            key={i}
            style={{ width: `${s.pct}%`, background: SEGMENT_VAR[s.color] }}
          />
        ))
      ) : (
        <div className="meter__fill" style={{ width: `${clamp(pct ?? 0)}%` }} />
      )}
    </div>
  );
}

function clamp(pct: number): number {
  return Math.max(0, Math.min(100, pct));
}
