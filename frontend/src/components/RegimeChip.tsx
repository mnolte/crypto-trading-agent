import type { Regime } from "../api/client";

/**
 * The regime pill used on the Dashboard, the decision drawer and Indicators:
 * "up · normal vol · 14 bars". A pair still backfilling has no regime yet.
 */
export function RegimeChip({
  regime,
  label,
}: {
  regime: Regime | null | undefined;
  /** e.g. "regime" or "BTC" prepended to the text */
  label?: string;
}) {
  if (!regime) {
    return (
      <span className="regime-chip regime-chip--warm">
        {label ? `${label}: ` : ""}warming up, no regime yet
      </span>
    );
  }
  const text =
    `${label ? `${label} ` : ""}${regime.trend} · ${regime.volatility} vol · ` +
    `${regime.age_4h} bars${regime.age_4h < 6 ? " · new" : ""}`;
  return (
    <span
      className={`regime-chip regime-chip--${regime.trend}`}
      title="Trend from 4h and daily EMAs with 4h ADX ≥ 25; volatility from the 1h ATR percentile; age in closed 4h bars"
    >
      {text}
    </span>
  );
}
