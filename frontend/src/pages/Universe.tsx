import { Fragment, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type {
  CheckRunOut,
  EntryPause,
  SyncJobOut,
  UniverseMarketOut,
} from "../api/client";
import {
  useRetrySyncJob,
  useSettings,
  useSnapshot,
  useUniverse,
  useUniverseCheck,
  useUniverseSync,
  useUpdateMarket,
} from "../api/hooks";
import { AppShell } from "../components/AppShell";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Chip } from "../components/Chip";
import { Input } from "../components/FormField";
import { KpiTile } from "../components/KpiTile";
import { PauseSelect } from "../components/PauseSelect";
import { RegimeChip } from "../components/RegimeChip";
import { SegmentedControl } from "../components/SegmentedControl";
import {
  formatAge,
  formatChange,
  formatDayShort,
  formatPct,
  formatPrice,
  formatTime,
} from "../lib/format";
import {
  backfillResult,
  baseSymbol,
  filterMatches,
  foldGroups,
  foldLabel,
  formatRunSeconds,
  funnelLabel,
  jobResultText,
  parseFilter,
  sortMarkets,
  statusChip,
  statusLine,
  syncAttention,
  syncRows,
  syncSummary,
  syncTotals,
  UNIVERSE_FILTERS,
  type MarketGroup,
  type SortKey,
  type SyncRow,
  type SyncTotals,
  type UniverseFilter,
} from "./universe";

const DISPLAY_TZ = "Europe/Amsterdam";

function nowMs(): number {
  return Date.now();
}

/** "03:00" — the Amsterdam clock time of the 01:00 UTC daily check,
 * following summer time. */
function checkTimeAmsterdam(): string {
  const now = new Date();
  const utc = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 1, 0),
  );
  return utc.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: DISPLAY_TZ,
  });
}

function hms(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone: DISPLAY_TZ,
  });
}

function count(value: number): string {
  return value.toLocaleString("en-GB");
}

/** 30-day volume as millions with up to 2 decimals: "6.8", "182.4", "0.31". */
function volumeMillions(value: string | null): string {
  if (value === null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return (n / 1_000_000).toLocaleString("en-GB", {
    maximumFractionDigits: 2,
  });
}

function corrText(value: string | null): string {
  if (value === null) return "—";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : "—";
}

// ------------------------------------------------------------------ tiles --

type UniverseData = NonNullable<ReturnType<typeof useUniverse>["data"]>;
type UniverseSyncData = NonNullable<ReturnType<typeof useUniverseSync>["data"]>;
type UniverseCheckData = NonNullable<ReturnType<typeof useUniverseCheck>["data"]>;

function Tiles({
  data,
  failDays,
}: {
  data: UniverseData;
  failDays: number;
}) {
  const { tiles, calls } = data;
  const failing = tiles.failing as {
    count?: number;
    names?: string[];
    days?: Record<string, number>;
  };
  const warming = tiles.warming as {
    count?: number;
    first?: string | null;
    reason?: string | null;
  };
  const failingCount = failing.count ?? 0;
  const firstFailing = failing.names?.[0];
  const failingSub =
    failingCount === 0 || !firstFailing
      ? "—"
      : `${baseSymbol(firstFailing)} · ${failing.days?.[firstFailing] ?? "?"} of ${failDays} days${
          failingCount > 1 ? ` · +${failingCount - 1} more` : ""
        }`;
  const warmingCount = warming.count ?? 0;
  const warmingSub =
    warmingCount === 0 || !warming.first
      ? "—"
      : `${baseSymbol(warming.first)} · ${
          warming.reason ? `${warming.reason} history failed` : "loading history"
        }`;

  const amber = { color: "var(--ta-warning-text)" };
  const green = { color: "var(--ta-positive)" };
  return (
    <div className="kpi-strip" data-testid="universe-tiles">
      <KpiTile
        label="EUR markets"
        value={count(tiles.eur_markets)}
        sub={`on Bitvavo · ${tiles.eur_excluded} excluded`}
      />
      <KpiTile
        label="Eligible"
        value={
          <span style={tiles.eligible > 0 ? green : undefined}>
            {count(tiles.eligible)}
          </span>
        }
        sub="for the next day"
      />
      <KpiTile
        label="Active"
        value={count(tiles.active)}
        sub={`up to ${count(tiles.active_calls_per_day)} calls a day · budget ${count(calls.daily_call_budget)}`}
      />
      <KpiTile
        label="Held"
        value={count(tiles.held.length)}
        sub={tiles.held.length ? tiles.held.join(", ") : "—"}
      />
      <KpiTile
        label="Failing the check"
        value={
          <span style={failingCount > 0 ? amber : undefined}>
            {count(failingCount)}
          </span>
        }
        sub={failingSub}
      />
      <KpiTile
        label="Warming up"
        value={
          <span style={warmingCount > 0 ? amber : undefined}>
            {count(warmingCount)}
          </span>
        }
        sub={warmingSub}
      />
    </div>
  );
}

// ------------------------------------------------------------ Data sync --

function dotClass(job: SyncJobOut | null, watcherErrors: number): string {
  if (job === null)
    return watcherErrors > 0 ? "un-dot un-dot--amber" : "un-dot un-dot--ok";
  if (job.status === "running") return "un-dot un-dot--run";
  if (job.status === "failed") return "un-dot un-dot--fail";
  if (job.status === "partial") return "un-dot un-dot--amber";
  return "un-dot un-dot--ok";
}

/** The row dot for the grouped History row: worst state wins. */
function backfillDot(jobs: SyncJobOut[]): string {
  if (jobs.some((j) => j.status === "failed")) return "un-dot un-dot--fail";
  if (jobs.some((j) => j.status === "running")) return "un-dot un-dot--run";
  if (jobs.some((j) => j.status === "partial")) return "un-dot un-dot--amber";
  return "un-dot un-dot--ok";
}

function retryKey(job: SyncJobOut): string {
  return job.market ? `${job.job}:${job.market}` : job.job;
}

function runStamp(job: SyncJobOut): { text: string; running: boolean } {
  const at = job.finished_at ?? job.started_at;
  const running = job.status === "running" && job.finished_at === null;
  return {
    text: at ? formatTime(at) : "—",
    running,
  };
}

function BackfillProgress({ job, extra }: { job: SyncJobOut; extra: number }) {
  const progress = job.progress as
    | { loaded?: number; needed?: number; eta_seconds?: number | null }
    | null
    | undefined;
  const loaded = progress?.loaded ?? 0;
  const needed = progress?.needed ?? 0;
  const pct = needed > 0 ? Math.min(100, (loaded / needed) * 100) : 0;
  const eta = progress?.eta_seconds;
  return (
    <div className="un-sync-progress">
      <div className="un-sync-progress__bar" aria-hidden="true">
        <i style={{ width: `${pct}%` }} />
      </div>
      <div>
        Loading history · {job.market} · {count(loaded)} of {count(needed)}{" "}
        candles
        {eta !== null && eta !== undefined && eta > 0
          ? ` · about ${formatAge(eta)} left`
          : ""}
        {extra > 0 ? ` · +${extra} more loading` : ""}
      </div>
    </div>
  );
}

function SyncJobRow({
  row,
  watcher,
  heldPairs,
  totals,
  onRetry,
  retrying,
}: {
  row: SyncRow;
  watcher: { checked_at: string | null; errors: number } | null;
  heldPairs: string[];
  totals: SyncTotals;
  onRetry: (key: string) => void;
  retrying: boolean;
}) {
  const job = row.job;
  if (row.key === "watcher") {
    // Prices for stops: the exit watcher's own state, no stored rows.
    const errors = watcher?.errors ?? 0;
    return (
      <div className="un-sync-row">
        <div className="un-sync-main">
          <span className={dotClass(null, errors)} aria-hidden="true" />
          <div className="un-sync-name">
            <div>{row.label}</div>
            <div className="un-sync-sub">{row.sub}</div>
          </div>
        </div>
        <div className="un-sync-side">
          <span className="num">{hms(watcher?.checked_at)}</span>
        </div>
        <div className="un-sync-result">
          {heldPairs.length
            ? `${heldPairs.length} held · ${heldPairs.join(", ")}`
            : "nothing held"}
          {errors > 0 ? ` · ${errors} error${errors === 1 ? "" : "s"}` : ""}
        </div>
      </div>
    );
  }

  if (row.key === "backfill") {
    const jobs = row.backfills;
    const running = jobs.filter((j) => j.status === "running");
    const barJob = running[0];
    const segmentJobs = jobs.filter((j) => j !== barJob);
    const latest = jobs.reduce<SyncJobOut | null>((acc, j) => {
      if (acc === null) return j;
      const ta = acc.finished_at ?? acc.started_at ?? "";
      const tb = j.finished_at ?? j.started_at ?? "";
      return tb > ta ? j : acc;
    }, null);
    const stamp = latest ? runStamp(latest) : { text: "—", running: false };
    const canRetry =
      running.length === 0 &&
      jobs.some((j) => j.status === "failed" || j.status === "partial");
    return (
      <div className="un-sync-row">
        <div className="un-sync-main">
          <span className={backfillDot(jobs)} aria-hidden="true" />
          <div className="un-sync-name">
            <div>{row.label}</div>
            <div className="un-sync-sub">{row.sub}</div>
          </div>
        </div>
        <div className="un-sync-side">
          <span className="num">
            {stamp.text}
            {latest?.duration_seconds != null
              ? ` · ${formatRunSeconds(latest.duration_seconds)}`
              : running.length > 0
                ? " · running"
                : ""}
          </span>
          {canRetry && (
            <Button
              size="xs"
              variant="secondary"
              disabled={retrying}
              onClick={() => onRetry("backfill")}
            >
              Retry
            </Button>
          )}
        </div>
        <div className="un-sync-result">
          {barJob && (
            <BackfillProgress job={barJob} extra={running.length - 1} />
          )}
          {segmentJobs.length > 0 && (
            <div>{backfillResult(segmentJobs)}</div>
          )}
          {segmentJobs.length === 0 && running.length === 0 && "no runs yet"}
        </div>
      </div>
    );
  }

  if (job === null) {
    return (
      <div className="un-sync-row">
        <div className="un-sync-main">
          <span className="un-dot" aria-hidden="true" />
          <div className="un-sync-name">
            <div>{row.label}</div>
            <div className="un-sync-sub">{row.sub}</div>
          </div>
        </div>
        <div className="un-sync-side">
          <span className="num">—</span>
        </div>
        <div className="un-sync-result">no run yet</div>
      </div>
    );
  }

  const stamp = runStamp(job);
  const canRetry =
    job.status !== "running" &&
    job.failures.length > 0 &&
    (job.status === "failed" || job.status === "partial");

  return (
    <div className="un-sync-row">
      <div className="un-sync-main">
        <span className={dotClass(job, 0)} aria-hidden="true" />
        <div className="un-sync-name">
          <div>{row.label}</div>
          <div className="un-sync-sub">{row.sub}</div>
        </div>
      </div>
      <div className="un-sync-side">
        <span className="num">
          {stamp.text}
          {job.duration_seconds !== null
            ? ` · ${formatRunSeconds(job.duration_seconds)}`
            : stamp.running
              ? " · running"
              : ""}
        </span>
        {canRetry && (
          <Button
            size="xs"
            variant="secondary"
            disabled={retrying}
            onClick={() => onRetry(retryKey(job))}
          >
            Retry
          </Button>
        )}
      </div>
      <div className="un-sync-result">{jobResultText(job, totals)}</div>
    </div>
  );
}

function DataSyncCard({
  data,
  heldPairs,
  perMarketCalls,
  totals,
}: {
  data: UniverseSyncData;
  heldPairs: string[];
  perMarketCalls: number;
  totals: SyncTotals;
}) {
  const retry = useRetrySyncJob();
  const [openOverride, setOpenOverride] = useState<boolean | null>(null);
  const cycleMinutes = perMarketCalls > 0 ? Math.round(1440 / perMarketCalls) : 15;
  const rows = useMemo(
    () => syncRows(data.jobs, cycleMinutes),
    [data.jobs, cycleMinutes],
  );
  const attention = useMemo(() => syncAttention(rows, nowMs()), [rows]);
  const attentionCount = attention.failed + attention.running + attention.late;
  const open = openOverride ?? attentionCount > 0;

  const lastRun = useMemo(() => {
    let latest: string | null = null;
    for (const j of data.jobs) {
      const t = j.finished_at ?? j.started_at;
      if (t && (latest === null || t > latest)) latest = t;
    }
    const w = data.watcher?.checked_at;
    if (w && (latest === null || w > latest)) latest = w;
    return latest;
  }, [data]);

  const summary = syncSummary(attention, rows.length);
  const headerText = open
    ? summary
    : `${summary}${lastRun ? ` · last ${formatTime(lastRun)}` : ""}`;

  return (
    <Card
      title="Data sync"
      label="Data sync"
      actions={
        <span className="un-sync-head">
          <span className="card__meta">{headerText}</span>
          <Button
            size="xs"
            variant="ghost"
            onClick={() => setOpenOverride(!open)}
            aria-expanded={open}
          >
            {open ? "Hide jobs" : "Show jobs"}
          </Button>
        </span>
      }
    >
      {open && (
        <>
          <div className="un-sync-rows">
            {rows.map((row) => (
              <SyncJobRow
                key={row.key}
                row={row}
                watcher={data.watcher}
                heldPairs={heldPairs}
                totals={totals}
                retrying={retry.isPending}
                onRetry={(key) => retry.mutate(key)}
              />
            ))}
          </div>
          <p className="card__meta" style={{ margin: "8px 0 0" }}>
            Requests stay far below Bitvavo's weight limit; a 429 waits for the
            time Bitvavo gives. Times in Amsterdam.
          </p>
        </>
      )}
    </Card>
  );
}

// --------------------------------------------------------- Today's check --

function keptText(
  run: CheckRunOut,
  markets: UniverseMarketOut[],
  thresholds: Record<string, unknown>,
): string | null {
  const byMarket = new Map(markets.map((m) => [m.market, m]));
  const failDays =
    typeof thresholds.eligibility_fail_days === "number"
      ? thresholds.eligibility_fail_days
      : 3;
  const recoverDays =
    typeof thresholds.eligibility_recover_days === "number"
      ? thresholds.eligibility_recover_days
      : 3;
  const parts: string[] = [];
  const kept = run.kept
    .map((m) => {
      const row = byMarket.get(m);
      const suffix =
        row && row.below_streak > 0
          ? ` (fail ${row.below_streak} of ${failDays})`
          : "";
      return `${baseSymbol(m)} kept${suffix}`;
    })
    .join(", ");
  if (kept) parts.push(`+ ${kept}`);
  const heldBack = run.held_back
    .map((m) => {
      const row = byMarket.get(m);
      const suffix =
        row && row.within_streak > 0
          ? ` (passing day ${row.within_streak} of ${recoverDays})`
          : "";
      return `${baseSymbol(m)} not yet${suffix}`;
    })
    .join(", ");
  if (heldBack) parts.push(`− ${heldBack}`);
  return parts.length ? parts.join(" · ") : null;
}

function HowCheckWorks({ thresholds }: { thresholds: Record<string, unknown> }) {
  const num = (key: string, fallback: number) => {
    const v = thresholds[key];
    return typeof v === "number" ? v : fallback;
  };
  const text = (key: string, fallback: string) => {
    const v = thresholds[key];
    return typeof v === "string" || typeof v === "number" ? String(v) : fallback;
  };
  const candles = num("min_daily_candles", 300);
  const failDays = num("eligibility_fail_days", 3);
  const beyond = text("eligibility_beyond_pct", "20");
  const recoverDays = num("eligibility_recover_days", 3);
  const volumeEur = (() => {
    const v = thresholds.min_avg_daily_volume_eur;
    const n = typeof v === "string" || typeof v === "number" ? Number(v) : NaN;
    if (!Number.isFinite(n)) return "1";
    const under = (n * (1 - Number(beyond) / 100)) / 1_000_000;
    return under >= 10 ? under.toFixed(0) : under.toFixed(1).replace(/\.0$/, "");
  })();
  const dayWord = (n: number) => (n === 1 ? "day" : "days");
  return (
    <details className="un-how">
      <summary>How the check works</summary>
      <ul>
        <li>
          Volume and spread come from the last 30 daily candles and Bitvavo's
          24h ticker; the {candles} daily candles are counted only for markets
          that pass both.
        </li>
        <li>
          An eligible market loses its place after {failDays} failing{" "}
          {dayWord(failDays)} in a row, or at once when it is {beyond}% past a
          line (under €{volumeEur}M volume) or no longer trading. It then needs{" "}
          {recoverDays} passing {dayWord(recoverDays)} in a row to return.
        </li>
        <li>
          A failing active market stays active and takes no new entries until
          you deactivate it or it recovers. An open position is still managed.
        </li>
        <li>
          If the check itself fails, the previous result stays and a banner
          says so.
        </li>
      </ul>
    </details>
  );
}

function CheckCard({
  detail,
  markets,
}: {
  detail: UniverseCheckData;
  markets: UniverseMarketOut[];
}) {
  const run =
    detail.latest && detail.latest.status === "ok"
      ? detail.latest
      : (detail.in_use ?? detail.latest);
  if (!run) {
    return (
      <Card title="Today's check" label="Today's check">
        <p className="card__meta" style={{ margin: 0 }}>
          No check has run yet.
        </p>
      </Card>
    );
  }
  const funnel = run.funnel;
  const first = funnel[0];
  const last = funnel[funnel.length - 1];
  const total = first?.left ?? 0;
  const kept = keptText(run, markets, run.thresholds);

  return (
    <Card
      title="Today's check"
      label="Today's check"
      actions={
        <span className="card__meta">
          {formatTime(run.finished_at ?? run.started_at)}
          {first && last ? ` · ${first.left} → ${last.left}` : ""}
        </span>
      }
    >
      <div className="un-funnel">
        {funnel.map((step, i) => {
          const pct = total > 0 ? (step.left / total) * 100 : 0;
          const droppedText =
            step.dropped.length === 0
              ? null
              : step.dropped.length <= 3
                ? step.dropped.map(baseSymbol).join(", ")
                : `${step.dropped.slice(0, 3).map(baseSymbol).join(", ")} …`;
          return (
            <div key={`${step.rule}-${i}`} className="un-funnel__row">
              <div className="un-funnel__label">
                {funnelLabel(step.rule, step.label, run.thresholds)}
                {droppedText && (
                  <span className="un-funnel__dropped">
                    {step.dropped.length} out: {droppedText}
                  </span>
                )}
              </div>
              <div className="un-funnel__bar" aria-hidden="true">
                <i
                  className={
                    i === funnel.length - 1
                      ? "un-funnel__fill un-funnel__fill--last"
                      : "un-funnel__fill"
                  }
                  style={{ width: `${pct}%` }}
                />
              </div>
              <div className="un-funnel__count num">{step.left}</div>
            </div>
          );
        })}
      </div>
      <div className="un-eligible">
        <strong>Eligible for the next day</strong>
        {last ? ` · ${last.left}` : ""}
        {kept && <div className="card__meta">{kept}</div>}
      </div>
      <HowCheckWorks thresholds={run.thresholds} />
    </Card>
  );
}

// ------------------------------------------------------------ Market table --

const COLUMNS: {
  key: SortKey | null;
  label: string;
  numeric?: boolean;
  hint?: string;
}[] = [
  { key: "market", label: "Market" },
  { key: "status", label: "Status" },
  {
    key: "price",
    label: "Price €",
    numeric: true,
    hint: "last price, refreshed at most once a minute",
  },
  {
    key: "volume",
    label: "Volume €M",
    numeric: true,
    hint: "30-day average daily volume at the check",
  },
  {
    key: "spread",
    label: "Spread",
    numeric: true,
    hint: "average spread over 24h at the check",
  },
  {
    key: "vsbtc",
    label: "vs BTC",
    numeric: true,
    hint: "30-day return minus BTC-EUR's over the same days",
  },
  {
    key: "corr",
    label: "Corr. BTC",
    numeric: true,
    hint: "correlation of the 30 daily returns with BTC-EUR's",
  },
  { key: "regime", label: "Regime" },
  { key: null, label: "" },
];

function SortTh({
  label,
  sortKey,
  sort,
  onSort,
  numeric,
  hint,
}: {
  label: string;
  sortKey: SortKey | null;
  sort: { key: SortKey; dir: "asc" | "desc" } | null;
  onSort: (key: SortKey) => void;
  numeric?: boolean;
  hint?: string;
}) {
  if (sortKey === null) {
    return <th className={numeric ? "num" : undefined} />;
  }
  const active = sort?.key === sortKey;
  return (
    <th
      className={numeric ? "num" : undefined}
      aria-sort={
        active ? (sort?.dir === "asc" ? "ascending" : "descending") : undefined
      }
    >
      <button
        type="button"
        className="un-sort"
        title={hint}
        onClick={() => onSort(sortKey)}
      >
        {label}
        <span aria-hidden="true">
          {active ? (sort?.dir === "asc" ? " ↑" : " ↓") : ""}
        </span>
      </button>
    </th>
  );
}

function StatusCell({ market }: { market: UniverseMarketOut }) {
  const chip = statusChip(market);
  const tfs = market.status.facts.tfs as
    | Record<string, { have: number; need: number }>
    | undefined;
  return (
    <td className="un-status">
      <Chip variant={chip.variant}>{chip.text}</Chip>
      {market.status.kind === "warming_up" && tfs && (
        <div className="un-tfs">
          {Object.entries(tfs).map(([tf, v]) => (
            <div key={tf} className="un-tf">
              <span className="un-tf__tf">{tf}</span>
              <span className="un-tf__bar" aria-hidden="true">
                <i
                  style={{
                    width: `${v.need > 0 ? Math.min(100, (v.have / v.need) * 100) : 0}%`,
                  }}
                />
              </span>
              <span className="un-tf__count num">
                {v.have} of {v.need}
              </span>
            </div>
          ))}
        </div>
      )}
      <div className="un-status__line">{statusLine(market)}</div>
    </td>
  );
}

type ConfirmState = {
  market: UniverseMarketOut;
  action: "activate" | "deactivate";
};

function ConfirmRow({
  confirm,
  calls,
  history,
  error,
  pending,
  onCancel,
  onConfirm,
}: {
  confirm: ConfirmState;
  calls: UniverseData["calls"];
  history: { h1h: number; h4h: number; h1d: number };
  error: string | null;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { market, action } = confirm;
  const before = count(calls.active_per_day);
  const after = count(calls.active_per_day + calls.per_market_per_day);
  const budget = count(calls.daily_call_budget);
  return (
    <tr className="un-confirm-row">
      <td colSpan={COLUMNS.length}>
        <div
          className="un-confirm"
          role="alertdialog"
          aria-label={`${action === "activate" ? "Activate" : "Deactivate"} ${market.market}?`}
        >
          <div className="un-confirm__head">
            <strong>
              {action === "activate" ? "Activate" : "Deactivate"}{" "}
              {market.market}?
            </strong>
            {action === "activate" && <Chip variant="amber">looser</Chip>}
          </div>
          {action === "activate" ? (
            <p className="un-confirm__body">
              Loads {history.h1h} 1h, {history.h4h} 4h and {history.h1d} 1d
              candles, then trades from the next cycle. Up to{" "}
              {calls.per_market_per_day} more model calls a day: {before} →{" "}
              {after} of the {budget} budget; past the budget, decisions use
              Rules until 00:00. Saved as a new settings version with the
              reason "Activated {market.market}".
            </p>
          ) : (
            <p className="un-confirm__body">
              Deactivate {market.market}? No new entries; an open position is
              still managed by its stop, target, time stop and the model.
            </p>
          )}
          {error && (
            <p className="settings-error" role="alert">
              {error}
            </p>
          )}
          <div className="un-confirm__actions">
            <Button
              size="sm"
              variant="secondary"
              onClick={onCancel}
              disabled={pending}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              variant={action === "activate" ? "primary" : "danger-outline"}
              disabled={pending}
              onClick={onConfirm}
            >
              {action === "activate" ? "Activate" : "Deactivate"}
            </Button>
          </div>
        </div>
      </td>
    </tr>
  );
}

function MarketRow({
  market: m,
  pause,
  perMarketCalls,
  onActivate,
  onDeactivate,
}: {
  market: UniverseMarketOut;
  pause: EntryPause | undefined;
  perMarketCalls: number;
  onActivate: () => void;
  onDeactivate: () => void;
}) {
  const kind = m.status.kind;
  const showActivate = kind === "eligible" && !m.active;
  const showActiveActions = m.active;
  const vsNum = m.vs_btc_30d_pct !== null ? Number(m.vs_btc_30d_pct) : null;
  return (
    <tr>
      <td className="un-market">
        <span className="text--strong">{m.market}</span>
        <span className="un-market__name">{m.name ?? "name pending"}</span>
      </td>
      <StatusCell market={m} />
      <td className="num">
        {m.last_price !== null ? formatPrice(m.last_price) : "—"}
      </td>
      <td className="num">{volumeMillions(m.volume_30d_eur)}</td>
      <td className="num">
        {m.spread_pct !== null ? formatPct(m.spread_pct, 2) : "—"}
      </td>
      <td
        className={`num${vsNum !== null && vsNum < 0 ? " down" : vsNum !== null && vsNum > 0 ? " up" : ""}`}
      >
        {m.vs_btc_30d_pct !== null ? formatChange(m.vs_btc_30d_pct) : "—"}
      </td>
      <td className="num">{corrText(m.corr_btc_30d)}</td>
      <td>
        {m.regime ? <RegimeChip regime={m.regime} /> : "—"}
      </td>
      <td className="un-actions">
        {showActivate && (
          <span className="un-activate">
            <Button size="xs" variant="secondary" onClick={onActivate}>
              Activate
            </Button>
            <span className="card__meta">
              +{perMarketCalls} calls a day
            </span>
          </span>
        )}
        {showActiveActions && (
          <span className="un-active-actions">
            <PauseSelect pair={m.market} pause={pause} />
            <Button size="xs" variant="secondary" onClick={onDeactivate}>
              Deactivate
            </Button>
          </span>
        )}
        {!showActivate && !showActiveActions && "—"}
      </td>
    </tr>
  );
}

function MarketTable({
  markets,
  filter,
  query,
  sort,
  onSort,
  pausedMap,
  perMarketCalls,
  calls,
  history,
  updateMarket,
  confirm,
  setConfirm,
}: {
  markets: UniverseMarketOut[];
  filter: UniverseFilter;
  query: string;
  sort: { key: SortKey; dir: "asc" | "desc" } | null;
  onSort: (key: SortKey) => void;
  pausedMap: Map<string, EntryPause>;
  perMarketCalls: number;
  calls: UniverseData["calls"];
  history: { h1h: number; h4h: number; h1d: number };
  updateMarket: ReturnType<typeof useUpdateMarket>;
  confirm: ConfirmState | null;
  setConfirm: (c: ConfirmState | null) => void;
}) {
  const [expanded, setExpanded] = useState<Set<MarketGroup>>(new Set());
  const [actionError, setActionError] = useState<string | null>(null);

  const folded = filter === "all" && query === "" && sort === null;
  const groups = useMemo(
    () => (folded ? foldGroups(markets) : null),
    [folded, markets],
  );

  type Item =
    | { type: "market"; market: UniverseMarketOut }
    | { type: "fold"; group: MarketGroup; hidden: UniverseMarketOut[] };
  const items: Item[] = useMemo(() => {
    if (!groups) {
      return markets.map((m) => ({ type: "market" as const, market: m }));
    }
    const out: Item[] = [];
    for (const g of groups) {
      for (const m of g.shown) out.push({ type: "market", market: m });
      if (g.hidden.length > 0) {
        if (expanded.has(g.group)) {
          for (const m of g.hidden) out.push({ type: "market", market: m });
        } else {
          out.push({ type: "fold", group: g.group, hidden: g.hidden });
        }
      }
    }
    return out;
  }, [groups, markets, expanded]);

  const toggleGroup = (group: MarketGroup) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(group)) next.delete(group);
      else next.add(group);
      return next;
    });

  const openConfirm = (market: UniverseMarketOut, action: ConfirmState["action"]) => {
    setActionError(null);
    setConfirm({ market, action });
  };

  const runAction = () => {
    if (!confirm) return;
    const active = confirm.action === "activate";
    setActionError(null);
    updateMarket.mutate(
      { market: confirm.market.market, active, confirmLooser: active },
      {
        onSuccess: () => setConfirm(null),
        onError: (e) => setActionError(e.message),
      },
    );
  };

  return (
    <div className="table-scroll un-table-wrap">
      <table className="dtable un-table">
        <thead>
          <tr>
            {COLUMNS.map((c, i) => (
              <SortTh
                key={c.label || `actions-${i}`}
                label={c.label}
                sortKey={c.key}
                sort={sort}
                onSort={onSort}
                numeric={c.numeric}
                hint={c.hint}
              />
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            if (item.type === "fold") {
              return (
                <tr key={`fold-${item.group}`} className="un-fold-row">
                  <td colSpan={COLUMNS.length}>
                    <button
                      type="button"
                      className="un-fold"
                      onClick={() => toggleGroup(item.group)}
                    >
                      {foldLabel(item.group, item.hidden)} · show all
                    </button>
                  </td>
                </tr>
              );
            }
            const m = item.market;
            return (
              <Fragment key={m.market}>
                <MarketRow
                  market={m}
                  pause={pausedMap.get(m.market) ?? pausedMap.get("*")}
                  perMarketCalls={perMarketCalls}
                  onActivate={() => openConfirm(m, "activate")}
                  onDeactivate={() => openConfirm(m, "deactivate")}
                />
                {confirm?.market.market === m.market && (
                  <ConfirmRow
                    confirm={confirm}
                    calls={calls}
                    history={history}
                    error={actionError}
                    pending={updateMarket.isPending}
                    onCancel={() => setConfirm(null)}
                    onConfirm={runAction}
                  />
                )}
              </Fragment>
            );
          })}
          {items.length === 0 && (
            <tr>
              <td colSpan={COLUMNS.length} className="card__meta">
                No markets match.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ page --

export function Universe() {
  const universe = useUniverse();
  const checkDetail = useUniverseCheck();
  const sync = useUniverseSync();
  const settings = useSettings();
  const snapshot = useSnapshot();
  const updateMarket = useUpdateMarket();
  const [params, setParams] = useSearchParams();
  const filter = parseFilter(params.get("filter"));
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{
    key: SortKey;
    dir: "asc" | "desc";
  } | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);

  const data = universe.data;
  const markets = useMemo(() => data?.markets ?? [], [data]);

  const pausedMap = useMemo(
    () => new Map((snapshot.data?.paused ?? []).map((p) => [p.pair, p])),
    [snapshot.data],
  );

  const counts = useMemo(() => {
    const c = {} as Record<UniverseFilter, number>;
    c.all = markets.length;
    for (const f of UNIVERSE_FILTERS) {
      if (f.id !== "all") c[f.id] = markets.filter((m) => filterMatches(m, f.id)).length;
    }
    return c;
  }, [markets]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    let rows = markets.filter((m) => filterMatches(m, filter));
    if (q) {
      rows = rows.filter(
        (m) =>
          m.market.toLowerCase().includes(q) ||
          (m.name ?? "").toLowerCase().includes(q),
      );
    }
    if (sort) rows = sortMarkets(rows, sort.key, sort.dir);
    return rows;
  }, [markets, filter, query, sort]);

  const onSort = (key: SortKey) =>
    setSort((prev) =>
      prev?.key === key
        ? prev.dir === "asc"
          ? { key, dir: "desc" }
          : null
        : { key, dir: "asc" },
    );

  const setFilter = (value: string) => {
    const next = new URLSearchParams(params);
    if (value === "all") next.delete("filter");
    else next.set("filter", value);
    setParams(next, { replace: true });
  };

  const check = data?.check;
  const checkFailed = check?.status === "failed";
  const amsCheck = checkTimeAmsterdam();

  const marketCfg = settings.data?.market;
  const history = {
    h1h: marketCfg?.history_1h ?? 800,
    h4h: marketCfg?.history_4h ?? 300,
    h1d: marketCfg?.history_1d ?? 300,
  };
  const calls = data?.calls ?? {
    per_market_per_day: 96,
    active_per_day: 0,
    daily_call_budget: 1000,
  };
  const heldPairs = data?.tiles.held ?? [];
  const totals = useMemo(
    () => (data ? syncTotals(markets, data.tiles) : null),
    [data, markets],
  );

  return (
    <AppShell
      title="Universe"
      sub={
        data
          ? `${count(data.tiles.eur_markets)} Bitvavo EUR markets · ${count(data.tiles.eligible)} eligible · ${count(data.tiles.active)} active`
          : undefined
      }
    >
      <Banner
        variant={checkFailed ? "warning" : "info"}
        actions={
          <Link className="btn btn--secondary btn--sm" to="/settings/markets">
            Check settings
          </Link>
        }
      >
        Every Bitvavo EUR market is checked at 01:00 UTC ({amsCheck} Amsterdam)
        and at startup. You choose which eligible markets are active; nothing
        is activated or deactivated automatically. Open positions are always
        managed, even on a market that is deactivated or no longer eligible.
        {checkFailed && check?.last_check_at && (
          <>
            {" "}
            The check at {formatTime(check.last_check_at)} failed
            {check.error ? `: ${check.error}` : ""}.{" "}
            {check.result_in_use_at
              ? `Using the result of ${formatDayShort(check.result_in_use_at)} ${formatTime(check.result_in_use_at)}.`
              : "No earlier result is available."}
          </>
        )}
      </Banner>

      {data && (
        <Tiles
          data={data}
          failDays={settings.data?.market.eligibility_fail_days ?? 3}
        />
      )}

      <div className="row">
        <div className="un-half">
          {sync.data ? (
            <DataSyncCard
              data={sync.data}
              heldPairs={heldPairs}
              perMarketCalls={calls.per_market_per_day}
              totals={
                totals ?? {
                  eurMarkets: 0,
                  eligible: 0,
                  failing: 0,
                  recovering: 0,
                  active: 0,
                }
              }
            />
          ) : (
            <Card title="Data sync" label="Data sync">
              <p className="card__meta" style={{ margin: 0 }}>
                Loading…
              </p>
            </Card>
          )}
        </div>
        <div className="un-half">
          {checkDetail.data ? (
            <CheckCard detail={checkDetail.data} markets={markets} />
          ) : (
            <Card title="Today's check" label="Today's check">
              <p className="card__meta" style={{ margin: 0 }}>
                Loading…
              </p>
            </Card>
          )}
        </div>
      </div>

      <Card
        title="Markets"
        label="Markets"
        actions={
          <div className="un-table-tools">
            <SegmentedControl
              label="Filter markets"
              value={filter}
              onChange={setFilter}
              options={UNIVERSE_FILTERS.map((f) => ({
                value: f.id,
                label: `${f.label} ${counts[f.id]}`,
              }))}
            />
            <Input
              type="search"
              placeholder="Find a market, e.g. NEAR"
              aria-label="Find a market"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="un-search"
            />
          </div>
        }
      >
        {sort === null && (
          <p className="card__meta" style={{ margin: "0 0 8px" }}>
            Active first (those that need a look on top), then eligible, then by
            volume · click a column to sort
          </p>
        )}
        {data ? (
          <MarketTable
            markets={visible}
            filter={filter}
            query={query}
            sort={sort}
            onSort={onSort}
            pausedMap={pausedMap}
            perMarketCalls={calls.per_market_per_day}
            calls={calls}
            history={history}
            updateMarket={updateMarket}
            confirm={confirm}
            setConfirm={setConfirm}
          />
        ) : (
          <p className="card__meta">Loading markets…</p>
        )}
      </Card>
    </AppShell>
  );
}
