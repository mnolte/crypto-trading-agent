import type { ReactNode } from "react";

export type ChipVariant = "neutral" | "up" | "down" | "amber" | "side";

export function Chip({
  variant = "neutral",
  pill = false,
  title,
  children,
}: {
  variant?: ChipVariant;
  /** Bordered rounded style used by the regime chips. */
  pill?: boolean;
  title?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={`chip chip--${variant}${pill ? " chip--pill" : ""}`}
      title={title}
    >
      {children}
    </span>
  );
}

const TREND_VARIANT: Record<string, ChipVariant> = {
  up: "up",
  down: "down",
  sideways: "side",
};

/** Regime label as it appears next to decisions: trend · vol · age. */
export function RegimeChip({
  trend,
  volatility,
  age4h,
  label,
}: {
  trend: "up" | "down" | "sideways";
  volatility: "low" | "normal" | "high";
  age4h: number;
  label?: string;
}) {
  const text = `${label ? `${label} ` : ""}${trend} · ${volatility} vol · ${age4h} bars${age4h < 6 ? " · new" : ""}`;
  return (
    <Chip variant={TREND_VARIANT[trend]} pill title="Market regime: trend from 4h and daily, volatility from the 1h ATR percentile, age in 4h bars">
      {text}
    </Chip>
  );
}
