import type {
  MarketStatusKind,
  SyncJobOut,
  UniverseMarketOut,
} from "../api/client";
import { formatTime } from "../lib/format";

/** The seven filters of the market table; the id is the `?filter=` value. */
export type UniverseFilter =
  | "all"
  | "active"
  | "held"
  | "eligible"
  | "failing"
  | "warming"
  | "excluded";

export const UNIVERSE_FILTERS: { id: UniverseFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "held", label: "Held" },
  { id: "eligible", label: "Eligible" },
  { id: "failing", label: "Failing" },
  { id: "warming", label: "Warming up" },
  { id: "excluded", label: "Excluded" },
];

export function parseFilter(value: string | null): UniverseFilter {
  return UNIVERSE_FILTERS.some((f) => f.id === value)
    ? (value as UniverseFilter)
    : "all";
}

/** A filter is not a partition: failing actives also count as Active and
 * Excluded shows every market that isn't eligible, recovering included. */
export function filterMatches(
  market: UniverseMarketOut,
  filter: UniverseFilter,
): boolean {
  switch (filter) {
    case "all":
      return true;
    case "active":
      return market.active;
    case "held":
      return market.status.held;
    case "eligible":
      return market.eligible;
    case "failing":
      return market.status.kind === "failing" || market.status.kind === "fails";
    case "warming":
      return market.status.kind === "warming_up";
    case "excluded":
      return !market.eligible;
  }
}

/** The fold groups of the default order: active, eligible, recovering,
 * excluded (held-but-not-active lands in excluded). */
export type MarketGroup = "active" | "eligible" | "recovering" | "excluded";

export function marketGroup(market: UniverseMarketOut): MarketGroup {
  if (market.active) return "active";
  if (market.eligible) return "eligible";
  if (market.status.kind === "recovering") return "recovering";
  return "excluded";
}

export const GROUP_ORDER: MarketGroup[] = [
  "active",
  "eligible",
  "recovering",
  "excluded",
];

/** Kinds that always stay visible inside the active fold. */
const ATTENTION_KINDS: MarketStatusKind[] = [
  "warming_up",
  "failing",
  "fails",
  "paused",
  "held",
];

export function needsLook(market: UniverseMarketOut): boolean {
  return market.active && ATTENTION_KINDS.includes(market.status.kind);
}

/** One fold group: the rows kept visible and the ones a "show all" row
 * would reveal. `markets` must already be in table order (the backend's
 * group order, or a column sort — folding only applies to the default
 * order anyway). */
export type FoldGroup = {
  group: MarketGroup;
  shown: UniverseMarketOut[];
  hidden: UniverseMarketOut[];
};

const FOLD_CAPS: Record<MarketGroup, number> = {
  active: 6,
  eligible: 2,
  recovering: Number.POSITIVE_INFINITY,
  excluded: 3,
};

/** Default fold: up to 6 active (attention always kept), 2 eligible,
 * every recovering, 3 excluded. */
export function foldGroups(markets: UniverseMarketOut[]): FoldGroup[] {
  const groups = new Map<MarketGroup, FoldGroup>();
  for (const group of GROUP_ORDER) {
    groups.set(group, { group, shown: [], hidden: [] });
  }
  for (const market of markets) {
    const group = marketGroup(market);
    const g = groups.get(group)!;
    if ((group === "active" && needsLook(market)) || g.shown.length < FOLD_CAPS[group]) {
      g.shown.push(market);
    } else {
      g.hidden.push(market);
    }
  }
  return GROUP_ORDER.map((g) => groups.get(g)!);
}

/** The "N more X: names · show all" fold-row text. Names show for small
 * hidden sets only (the design lists them for a handful, not 217). */
export function foldLabel(group: MarketGroup, hidden: UniverseMarketOut[]): string {
  const noun =
    group === "active"
      ? "active"
      : group === "eligible"
        ? "eligible"
        : group === "recovering"
          ? "recovering"
          : "excluded";
  const count = `${hidden.length} more ${noun}`;
  if (hidden.length > 10) return count;
  const names = hidden
    .slice(0, 8)
    .map((m) => baseSymbol(m.market))
    .join(", ");
  const more = hidden.length > 8 ? ", …" : "";
  return `${count}: ${names}${more}`;
}

/** A warming-up market's status line without the per-timeframe counts the
 * bars already show ("4h failed … · retry … · trades once loaded"). */
export function statusLine(market: UniverseMarketOut): string {
  const line = market.status.line;
  if (market.status.kind !== "warming_up") return line;
  const rest = line
    .split(" · ")
    .filter((part) => !/^\d+[hd] \d+ of \d+$/.test(part));
  return rest.join(" · ") || line;
}

export type ChipStyle = {
  variant: "neutral" | "up" | "down" | "amber" | "side" | "info" | "eligible";
  text: string;
};

function num(facts: Record<string, unknown>, key: string): number | null {
  const v = facts[key];
  return typeof v === "number" ? v : null;
}

/** The chip next to the status line; text mirrors the spec's names. */
export function statusChip(market: UniverseMarketOut): ChipStyle {
  const { kind, facts } = market.status;
  const fails = num(facts, "fails");
  const of = num(facts, "of");
  const passing = num(facts, "passing");
  const need = num(facts, "need");
  switch (kind) {
    case "warming_up":
      return { variant: "amber", text: "active · warming up" };
    case "failing":
      return { variant: "amber", text: "active · failing" };
    case "fails":
      return {
        variant: "amber",
        text:
          fails !== null && of !== null
            ? `active · ${fails} of ${of} fails`
            : "active · failing checks",
      };
    case "paused":
      return { variant: "neutral", text: "active · entries paused" };
    case "held":
      return { variant: "info", text: "active · held" };
    case "active":
      return { variant: "up", text: "active" };
    case "eligible":
      return { variant: "eligible", text: "eligible" };
    case "recovering":
      return {
        variant: "info",
        text:
          passing !== null && need !== null
            ? `recovering · ${passing} of ${need}`
            : "recovering",
      };
    case "excluded":
      return { variant: "down", text: "excluded" };
    case "held_not_active":
      return { variant: "info", text: "held · not active" };
  }
}

export type SortKey =
  | "market"
  | "status"
  | "price"
  | "volume"
  | "spread"
  | "vsbtc"
  | "corr"
  | "regime";

const KIND_RANK: Record<MarketStatusKind, number> = {
  warming_up: 0,
  failing: 1,
  fails: 2,
  paused: 3,
  held: 4,
  active: 5,
  eligible: 6,
  recovering: 7,
  excluded: 8,
  held_not_active: 9,
};

function numOrNull(value: string | null): number | null {
  if (value === null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function sortValue(market: UniverseMarketOut, key: SortKey): string | number | null {
  switch (key) {
    case "market":
      return market.market;
    case "status":
      return (
        GROUP_ORDER.indexOf(marketGroup(market)) * 10 +
        KIND_RANK[market.status.kind]
      );
    case "price":
      return numOrNull(market.last_price);
    case "volume":
      return numOrNull(market.volume_30d_eur);
    case "spread":
      return numOrNull(market.spread_pct);
    case "vsbtc":
      return numOrNull(market.vs_btc_30d_pct);
    case "corr":
      return numOrNull(market.corr_btc_30d);
    case "regime":
      return market.regime ? `${market.regime.trend}${market.regime.volatility}` : null;
  }
}

/** Sort a copy; nulls always last. Strings compare case-insensitively. */
export function sortMarkets(
  markets: UniverseMarketOut[],
  key: SortKey | null,
  dir: "asc" | "desc",
): UniverseMarketOut[] {
  if (key === null) return markets;
  const sign = dir === "asc" ? 1 : -1;
  return [...markets].sort((a, b) => {
    const va = sortValue(a, key);
    const vb = sortValue(b, key);
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    if (typeof va === "string" && typeof vb === "string") {
      return sign * va.localeCompare(vb);
    }
    return sign * (Number(va) - Number(vb));
  });
}

/** "ADA-EUR" -> "ADA" for fold-row name lists. */
export function baseSymbol(market: string): string {
  return market.replace(/-EUR$/, "");
}

// ------------------------------------------------------------- Data sync --

export type SyncRow = {
  key: string;
  label: string;
  sub: string;
  /** The stored run for the single-run jobs; null for the watcher row
   * (its state lives on the engine) and for the grouped backfill row. */
  job: SyncJobOut | null;
  /** Latest run per activated market, shown as the one "History" row. */
  backfills: SyncJobOut[];
};

export function syncRows(
  jobs: SyncJobOut[],
  cycleMinutes: number,
): SyncRow[] {
  const byJob = new Map(jobs.map((j) => [`${j.job}:${j.market ?? ""}`, j]));
  const row = (job: string): SyncJobOut | null =>
    byJob.get(`${job}:`) ?? null;
  const rows: SyncRow[] = [
    {
      key: "exchange_rules",
      label: "Exchange rules",
      sub: "01:00 UTC · and at startup",
      job: row("exchange_rules"),
      backfills: [],
    },
    {
      key: "daily_check",
      label: "Daily check",
      sub: "01:00 UTC · after the rules",
      job: row("daily_check"),
      backfills: [],
    },
    {
      key: "candles",
      label: "Candles",
      sub: `each cycle · every ${cycleMinutes} min`,
      job: row("candles"),
      backfills: [],
    },
    {
      key: "watcher",
      label: "Prices for stops",
      sub: "every 30 s · the exit watcher",
      job: null,
      backfills: [],
    },
  ];
  const backfills = jobs
    .filter((j) => j.job === "backfill")
    .sort((a, b) => (a.market ?? "").localeCompare(b.market ?? ""));
  rows.push({
    key: "backfill",
    label: "History for activated markets",
    sub: "when a market is activated",
    job: null,
    backfills,
  });
  return rows;
}

export type SyncAttention = {
  failed: number;
  running: number;
  late: number;
};

/** Twice the interval without a good (ok) run; the daily jobs get 26 h. */
const JOB_INTERVAL_MS: Record<string, number> = {
  exchange_rules: 26 * 3_600_000,
  daily_check: 26 * 3_600_000,
  candles: 30 * 60_000,
};

/** Counts display rows, not stored runs: the grouped backfill row counts
 * once however many markets it covers. */
export function syncAttention(
  rows: SyncRow[],
  now: number,
): SyncAttention {
  let failed = 0;
  let running = 0;
  let late = 0;
  for (const row of rows) {
    const jobs = row.backfills.length
      ? row.backfills
      : row.job
        ? [row.job]
        : [];
    let rowFailed = false;
    let rowRunning = false;
    let rowLate = false;
    for (const job of jobs) {
      if (job.status === "running") rowRunning = true;
      if (job.status === "failed" || job.status === "partial") rowFailed = true;
      const interval = JOB_INTERVAL_MS[job.job];
      if (
        interval !== undefined &&
        job.status === "ok" &&
        job.finished_at !== null &&
        now - new Date(job.finished_at).getTime() > interval
      ) {
        rowLate = true;
      }
    }
    if (rowFailed) failed += 1;
    if (rowRunning) running += 1;
    if (rowLate) late += 1;
  }
  return { failed, running, late };
}

export function syncSummary(attention: SyncAttention, total: number): string {
  if (attention.failed + attention.running + attention.late === 0) {
    return `All ${total} jobs ran on time`;
  }
  const parts: string[] = [];
  if (attention.failed) parts.push(`${attention.failed} failed`);
  if (attention.running) parts.push(`${attention.running} running`);
  if (attention.late) parts.push(`${attention.late} late`);
  return `open: ${parts.join(" · ")}`;
}

/** The counts the ok-state result lines quote (spec's "Row shows" column). */
export type SyncTotals = {
  eurMarkets: number;
  eligible: number;
  failing: number;
  recovering: number;
  active: number;
};

export function syncTotals(
  markets: UniverseMarketOut[],
  tiles: {
    eur_markets: number;
    eligible: number;
    active: number;
    failing: { count?: number };
  },
): SyncTotals {
  return {
    eurMarkets: tiles.eur_markets,
    eligible: tiles.eligible,
    failing: tiles.failing.count ?? 0,
    recovering: markets.filter((m) => m.status.kind === "recovering").length,
    active: tiles.active,
  };
}

function count(value: number): string {
  return value.toLocaleString("en-GB");
}

/** The result column of a single-run job row: the spec's summary for a
 * good run, done-of-total plus failures otherwise. */
export function jobResultText(job: SyncJobOut, totals?: SyncTotals): string {
  const parts: string[] = [];
  if (job.status === "ok" || job.status === "partial") {
    if (job.job === "exchange_rules" && totals) {
      parts.push(
        `${count(totals.eurMarkets)} EUR markets · order sizes and precision`,
      );
    } else if (job.job === "daily_check" && totals) {
      parts.push(
        `${count(totals.eurMarkets)} checked · ${count(totals.eligible)} eligible · ` +
          `${count(totals.failing)} failing · ${count(totals.recovering)} recovering`,
      );
    } else if (job.job === "candles" && totals) {
      parts.push(
        `${count(totals.active)} active markets · 1h each cycle, 4h and 1d each hour`,
      );
    }
  }
  if (parts.length === 0 && job.done !== null && job.total !== null) {
    parts.push(`${count(job.done)} of ${count(job.total)}`);
  }
  for (const f of job.failures) {
    const tf = f.tf ? ` ${f.tf}` : "";
    let text = `${f.what}${tf}: ${f.error}`;
    if (f.next_retry) text += ` · retry ${formatTime(f.next_retry)}`;
    parts.push(text);
  }
  if (parts.length === 0) {
    parts.push(job.status === "ok" ? "ok" : (job.status ?? "—"));
  }
  return parts.join(" · ");
}

function backfillRank(job: SyncJobOut): number {
  if (job.status === "running") return 0;
  if (job.status === "failed" || job.status === "partial") return 1;
  return 2;
}

/** One market's segment of the History row: "SUI-EUR · 1,100 of 1,400 ·
 * 4h failed: Bitvavo: too many requests (429) · retry 12:45". */
export function backfillSegment(job: SyncJobOut): string {
  const parts: string[] = [job.market ?? "market"];
  const progress = job.progress as
    | { loaded?: number; needed?: number }
    | null
    | undefined;
  if (job.status === "running") {
    if (progress?.loaded != null && progress?.needed != null) {
      parts.push(
        `loading ${count(progress.loaded)} of ${count(progress.needed)}`,
      );
    } else {
      parts.push("loading");
    }
  } else if (job.done !== null && job.total !== null) {
    parts.push(`${count(job.done)} of ${count(job.total)}`);
  }
  for (const f of job.failures) {
    const tf = f.tf ? `${f.tf} ` : "";
    let text = `${tf}failed: ${f.error}`;
    if (f.next_retry) text += ` · retry ${formatTime(f.next_retry)}`;
    parts.push(text);
  }
  return parts.join(" · ");
}

/** The History row's result: attention markets first, then the rest. */
export function backfillResult(jobs: SyncJobOut[]): string {
  const sorted = [...jobs].sort(
    (a, b) => backfillRank(a) - backfillRank(b),
  );
  return sorted.map(backfillSegment).join(" · ");
}

/** Seconds -> "1.1 s" / "41 s" for the run duration column. */
export function formatRunSeconds(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  return seconds < 10 ? `${seconds.toFixed(1)} s` : `${Math.round(seconds)} s`;
}

// ------------------------------------------------------------ Today's check --

function thrNum(thresholds: Record<string, unknown>, key: string): number | null {
  const v = thresholds[key];
  if (typeof v === "number") return v;
  if (typeof v === "string") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function thrText(thresholds: Record<string, unknown>, key: string): string | null {
  const v = thresholds[key];
  return typeof v === "string" || typeof v === "number" ? String(v) : null;
}

/** "1000000" -> "€1M", "800000" -> "€0.8M" for the funnel thresholds. */
export function eurMillions(value: unknown): string {
  const n = typeof value === "string" || typeof value === "number" ? Number(value) : NaN;
  if (!Number.isFinite(n)) return "—";
  const m = n / 1_000_000;
  const text = m >= 10 ? m.toFixed(0) : m.toFixed(1).replace(/\.0$/, "");
  return `€${text}M`;
}

/** The funnel bar label with the thresholds of that check run. */
export function funnelLabel(
  rule: string,
  label: string | null | undefined,
  thresholds: Record<string, unknown>,
): string {
  switch (rule) {
    case "listed":
      return "Bitvavo EUR markets";
    case "type":
      return "not a stablecoin or fiat token";
    case "status":
      return "trading, not halted or delisting";
    case "volume": {
      const eur = thrNum(thresholds, "min_avg_daily_volume_eur");
      return eur !== null
        ? `30-day average daily volume ≥ ${eurMillions(eur)}`
        : (label ?? "30-day average daily volume");
    }
    case "spread": {
      const pct = thrText(thresholds, "max_avg_spread_pct");
      return pct !== null
        ? `average spread over 24h ≤ ${pct}%`
        : (label ?? "average spread over 24h");
    }
    case "history": {
      const candles = thrNum(thresholds, "min_daily_candles");
      return candles !== null
        ? `${candles} daily candles`
        : (label ?? "daily candles");
    }
    case "all":
      return "pass every rule today";
    default:
      return label ?? rule;
  }
}
