import type { Regime } from "../api/client";

/**
 * The regime pill used on the Dashboard, the decision drawer and Indicators:
 * "up · normal vol · 14 bars". A pair still backfilling has no regime yet.
 */
export function RegimeChip({
  regime,
  label,
  compact = false,
}: {
  regime: Regime | null | undefined;
  /** e.g. "regime" or "BTC" prepended to the text */
  label?: string;
  compact?: boolean;
}) {
  if (!regime) {
    return (
      <span className="regime-chip regime-chip--warm">
        {label ? `${label}: ` : ""}
        {compact ? "warming up" : "warming up, no regime yet"}
      </span>
    );
  }
  const isNew = regime.age_4h < 6;
  const shortText = `${label ? `${label} ` : ""}${regime.trend} · ${regime.volatility} vol`;
  const text = compact
    ? shortText
    : `${shortText} · ${regime.age_4h} bars${isNew ? " · new" : ""}`;
  const title = compact
    ? `${regime.age_4h} closed 4h bars${isNew ? " · new: the regime changed less than 6 bars ago" : ""}`
    : "Trend from 4h and daily EMAs with 4h ADX ≥ 25; volatility from the 1h ATR percentile; age in closed 4h bars";
  return (
    <span
      className={`regime-chip regime-chip--${regime.trend}${compact ? " regime-chip--compact" : ""}`}
      title={title}
    >
      <span className="regime-chip__text">{text}</span>
    </span>
  );
}
