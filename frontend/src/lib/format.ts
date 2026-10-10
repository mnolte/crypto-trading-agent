/**
 * Formatting helpers. Money arrives as a decimal string from the API; the
 * frontend formats it and never does arithmetic on it.
 */

const DISPLAY_TZ = "Europe/Amsterdam";

/** "10412.8" -> "€10,412.80"; keeps a leading − outside the sign. */
export function formatEur(value: string | number, decimals = 2): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const fmt = new Intl.NumberFormat("en-GB", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  return `${n < 0 ? "−" : ""}€${fmt.format(Math.abs(n))}`;
}

/** "61240.5" -> "61,240.50" — a price without the currency sign. */
export function formatPrice(value: string | number, decimals = 2): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat("en-GB", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(n);
}

/** Always signed percent for money/price changes: 1.42 -> "+1.42%". */
export function formatChange(pct: string | number, decimals = 2): string {
  const n = Number(pct);
  if (!Number.isFinite(n)) return "—";
  return `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(decimals)}%`;
}

/** Signed money: "86.2" -> "+€86.20". */
export function formatSignedEur(value: string | number, decimals = 2): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n >= 0 ? "+" : "−"}${formatEur(Math.abs(n), decimals)}`;
}

/** "12:30" in Europe/Amsterdam from an ISO timestamp. */
export function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: DISPLAY_TZ,
  });
}

/** "09 Oct 14:05" in Europe/Amsterdam. */
export function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const day = d.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    timeZone: DISPLAY_TZ,
  });
  return `${day} ${formatTime(iso)}`;
}

/** Seconds -> "11:41" countdown text. */
export function formatCountdown(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) return "—";
  const s = Math.floor(totalSeconds);
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${String(rem).padStart(2, "0")}`;
}
