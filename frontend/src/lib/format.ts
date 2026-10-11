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

/** "12:30:04 CEST" in Europe/Amsterdam — the decision drawer's timestamp. */
export function formatTimeTz(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
    timeZone: DISPLAY_TZ,
  });
}

/** Seconds -> "11:41" countdown text. */
export function formatCountdown(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) return "—";
  const s = Math.floor(totalSeconds);
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${String(rem).padStart(2, "0")}`;
}

/** Unsigned percent for gauges: 67 -> "67%"; null-ish -> "—". */
export function formatPct(value: string | number | null | undefined, decimals = 0): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(decimals)}%`;
}

/** Signed points value for R multiples and ratios: 0.61 -> "+0.61". */
export function formatSigned(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined) return "—";
  if (!Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(decimals)}`;
}

/** Seconds -> "2h 10m" / "9 min" / "45 s" age text. */
export function formatAge(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) return "—";
  const s = Math.floor(totalSeconds);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h >= 24) {
    const d = Math.floor(h / 24);
    return m > 0 || h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
  }
  return m ? `${h}h ${m}m` : `${h}h`;
}

/** Compact euro amount for big volumes: 61200000 -> "€61.2M". */
export function formatEurCompact(value: string | number): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const sign = n < 0 ? "−" : "";
  const a = Math.abs(n);
  if (a >= 1_000_000) return `${sign}€${(a / 1_000_000).toFixed(1)}M`;
  if (a >= 1_000) return `${sign}€${(a / 1_000).toFixed(1)}k`;
  return `${sign}€${a.toFixed(0)}`;
}

/** Trim a decimal string's trailing zeros without arithmetic: "0.08070" -> "0.0807". */
export function formatQty(value: string): string {
  if (!value.includes(".")) return value;
  return value.replace(/0+$/, "").replace(/\.$/, "");
}

/** Numeric input parser: empty or non-finite input means "keep the old value". */
export function parseNumberInput(value: string): number | undefined {
  if (value === "") return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}
