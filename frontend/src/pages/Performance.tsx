import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import type { UTCTimestamp } from "lightweight-charts";
import type { TradeOut } from "../api/client";
import { usePerformance, useSnapshot, useTrades } from "../api/hooks";
import { RecordNotFound } from "./Errors";
import { AppShell } from "../components/AppShell";
import { Card } from "../components/Card";
import { PerfChart, type LinePoint } from "../components/Charts";
import { DataTable } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { Input } from "../components/FormField";
import { KpiTile } from "../components/KpiTile";
import { SegmentedControl } from "../components/SegmentedControl";
import {
  formatDateTime,
  formatDayLong,
  formatEur,
  formatPct,
  formatPrice,
  formatSigned,
  formatSignedEur,
} from "../lib/format";

type Period = "7d" | "30d" | "start" | "custom";

const PERIODS: { value: Period; label: string }[] = [
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
  { value: "start", label: "Since start" },
  { value: "custom", label: "Custom…" },
];

function daysAgoIso(days: number): string {
  return new Date(Date.now() - days * 86_400_000).toISOString();
}

export function Performance() {
  const [period, setPeriod] = useState<Period>("30d");
  const [customFrom, setCustomFrom] = useState(daysAgoIso(14).slice(0, 10));
  const [customEnd, setCustomEnd] = useState(new Date().toISOString().slice(0, 10));

  // Stable per period choice — a fresh Date.now() each render would change the
  // query key on every render and refetch in a loop.
  const [from, end] = useMemo<[string | undefined, string | undefined]>(() => {
    if (period === "7d") return [daysAgoIso(7), undefined];
    if (period === "30d") return [daysAgoIso(30), undefined];
    if (period === "custom")
      return [
        new Date(customFrom).toISOString(),
        new Date(`${customEnd}T23:59:59`).toISOString(),
      ];
    return [undefined, undefined];
  }, [period, customFrom, customEnd]);

  const { data: perf } = usePerformance(from, end);
  const { data: trades } = useTrades(from, end);
  const { data: snapshot } = useSnapshot();
  const accountDays = snapshot?.account_started_at
    ? Math.max(
        1,
        Math.ceil(
          (Date.now() - new Date(snapshot.account_started_at).getTime()) /
            86_400_000,
        ),
      )
    : null;
  const sub = snapshot?.account_started_at
    ? `Paper account #${snapshot.account_id} since ${formatDayLong(snapshot.account_started_at)} · ${accountDays} days`
    : `Paper account #${snapshot?.account_id ?? 1}`;

  // Deep link: /performance?trade=<id> scrolls to and highlights the row.
  const focusTrade = useSearchParams()[0].get("trade");
  const tradeMissing =
    !!focusTrade &&
    trades !== undefined &&
    !trades.items.some((t) => String(t.id) === focusTrade);
  useEffect(() => {
    if (focusTrade) {
      document
        .getElementById(`trade-${focusTrade}`)
        ?.scrollIntoView({ block: "center" });
    }
  }, [focusTrade, trades]);

  const equity: LinePoint[] = (perf?.equity ?? []).map((p) => ({
    time: (new Date(p.at).getTime() / 1000) as UTCTimestamp,
    value: Number(p.equity),
  }));
  const benchmark: LinePoint[] = (perf?.benchmark ?? []).map((p) => ({
    time: (new Date(p.time).getTime() / 1000) as UTCTimestamp,
    value: p.value ?? 0,
  }));
  const drawdown: LinePoint[] = (perf?.drawdown ?? []).map((p) => ({
    time: (new Date(p.at).getTime() / 1000) as UTCTimestamp,
    value: p.pct,
  }));

  const t = perf?.totals;

  if (tradeMissing) {
    return (
      <AppShell title="Performance" sub={sub}>
        <RecordNotFound kind="Trade" id={focusTrade ?? ""} />
      </AppShell>
    );
  }
  return (
    <AppShell title="Performance" sub={sub}>
      <div className="page-controls">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
          <SegmentedControl
            label="Period"
            value={period}
            onChange={(v) => setPeriod(v as Period)}
            options={PERIODS}
          />
          {period === "custom" && (
            <>
              <Input
                type="date"
                aria-label="From"
                value={customFrom}
                onChange={(e) => setCustomFrom(e.target.value)}
              />
              <Input
                type="date"
                aria-label="To"
                value={customEnd}
                onChange={(e) => setCustomEnd(e.target.value)}
              />
            </>
          )}
          <a
            className="btn btn--secondary"
            href="/api/export/trades.csv"
            download
          >
            Export trades (CSV)
          </a>
        </div>
      </div>

      <section aria-label="Totals" className="kpi-strip">
        <KpiTile
          label="Return"
          value={
            <span style={{ color: t?.return_pct != null && t.return_pct < 0 ? "var(--ta-negative-text)" : "var(--ta-positive)" }}>
              {t?.return_pct != null ? `${formatSigned(t.return_pct)}%` : "—"}
            </span>
          }
          sub={
            t?.benchmark_return_pct != null
              ? `holding BTC: ${formatSigned(t.benchmark_return_pct)}%`
              : "holding BTC: —"
          }
        />
        <KpiTile
          label="vs holding BTC"
          value={
            t?.vs_btc_pct != null ? (
              <span
                style={{
                  color: t.vs_btc_pct >= 0 ? "var(--ta-positive)" : "var(--ta-negative-text)",
                }}
              >
                {formatSigned(t.vs_btc_pct)} pts
              </span>
            ) : (
              "—"
            )
          }
          sub="the bar to beat"
        />
        <KpiTile
          label="Max drawdown"
          value={
            <span style={{ color: "var(--ta-negative-text)" }}>
              {t?.max_drawdown_pct != null
                ? `−${Math.abs(t.max_drawdown_pct).toFixed(1)}%`
                : "—"}
            </span>
          }
          sub={perf ? `kill at −${formatPct(perf.kill_line_pct)}` : undefined}
        />
        <KpiTile
          label="Trades"
          value={t ? String(t.trades) : "—"}
          sub={t ? `${t.open_now} open now` : undefined}
        />
        <KpiTile
          label="Win rate"
          value={t?.win_rate_pct != null ? formatPct(t.win_rate_pct) : "—"}
          sub={t ? `${t.wins} of ${t.trades}` : undefined}
        />
        <KpiTile
          label="Average R"
          value={t?.avg_r != null ? formatSigned(t.avg_r) : "—"}
          sub="per closed trade"
        />
        <KpiTile
          label="Profit factor"
          glossaryTerm="profit factor"
          value={t?.profit_factor != null ? t.profit_factor.toFixed(2) : "—"}
          sub="gross win ÷ gross loss"
        />
        <KpiTile
          label="Fees paid"
          value={t ? formatEur(t.fees_eur) : "—"}
          sub={
            t?.time_in_market_pct != null
              ? `time in market ${formatPct(t.time_in_market_pct)}`
              : undefined
          }
        />
      </section>

      <Card title="Equity vs holding BTC" label="Equity and drawdown">
        <div className="chart-legend">
          <span className="chart-legend__item">
            <span
              style={{
                width: 18,
                height: 3,
                background: "var(--ta-accent)",
                borderRadius: 2,
              }}
            />
            Agent
          </span>
          <span className="chart-legend__item">
            <span
              style={{ width: 18, borderTop: "2px dashed var(--ta-text-muted)" }}
            />
            same euros held in BTC
          </span>
        </div>
        {equity.length > 0 ? (
          <PerfChart
            equity={equity}
            benchmark={benchmark}
            drawdown={drawdown}
            killPct={perf?.kill_line_pct ?? 8}
          />
        ) : (
          <EmptyState
            title="No equity data yet"
            footer="The curve starts with the first cycle."
          />
        )}
        {perf && (
          <div
            className="num"
            style={{
              display: "flex",
              justifyContent: "space-between",
              fontSize: 12,
              color: "var(--ta-text-muted)",
            }}
          >
            <span>{formatDateTime(perf.start)}</span>
            <span>{formatDateTime(perf.end)}</span>
          </div>
        )}
      </Card>

      <div className="row">
        <Card title="Per pair" label="Per pair" className="row__side">
          {perf && perf.per_pair.length > 0 ? (
            <DataTable
              columns={[
                { key: "pair", label: "Pair" },
                { key: "trades", label: "Trades", numeric: true },
                { key: "pnl", label: "P&L", numeric: true },
                { key: "r", label: "Avg R", numeric: true },
                { key: "fees", label: "Fees", numeric: true },
              ]}
            >
              {perf.per_pair.map((p) => (
                <tr key={p.pair}>
                  <td className="text--strong">{p.pair}</td>
                  <td className="num">{p.trades}</td>
                  <td
                    className="num"
                    style={{
                      color:
                        Number(p.realised_eur) >= 0
                          ? "var(--ta-positive)"
                          : "var(--ta-negative-text)",
                    }}
                  >
                    {formatSignedEur(p.realised_eur)}
                  </td>
                  <td className="num">
                    {p.avg_r != null ? formatSigned(p.avg_r) : "—"}
                  </td>
                  <td className="num">{formatEur(p.fees_eur)}</td>
                </tr>
              ))}
            </DataTable>
          ) : (
            <EmptyState
              title="No closed trades"
              footer="Each pair's realised P&L appears after the first exit."
            />
          )}
        </Card>

        <Card title="Closed trades" label="Closed trades" className="row__main">
          {trades && trades.items.length > 0 ? (
            <DataTable
              minWidth={820}
              columns={[
                { key: "pair", label: "Pair" },
                { key: "when", label: "Opened → closed" },
                { key: "entry", label: "Entry", numeric: true },
                { key: "exit", label: "Exit", numeric: true },
                { key: "fees", label: "Fees", numeric: true },
                { key: "result", label: "Result", numeric: true },
                { key: "reason", label: "Exit reason" },
              ]}
            >
              {trades.items.map((trade) => (
                <TradeRow
                  key={trade.id}
                  trade={trade}
                  focus={focusTrade === String(trade.id)}
                />
              ))}
            </DataTable>
          ) : (
            <EmptyState
              title="No closed trades"
              footer="Fills and their results are listed here."
            />
          )}
        </Card>
      </div>
    </AppShell>
  );
}

function TradeRow({ trade, focus }: { trade: TradeOut; focus?: boolean }) {
  const pnl = Number(trade.realised_eur);
  return (
    <tr id={`trade-${trade.id}`} className={focus ? "row--focus" : undefined}>
      <td className="text--strong">{trade.pair}</td>
      <td className="num" style={{ color: "var(--ta-text-2)", fontFamily: "var(--ta-font-mono)" }}>
        {formatDateTime(trade.opened_at)} → {formatDateTime(trade.closed_at)}
      </td>
      <td className="num">{formatPrice(trade.entry)}</td>
      <td className="num">{formatPrice(trade.exit)}</td>
      <td className="num">{formatEur(trade.fee_eur)}</td>
      <td
        className="num"
        style={{ color: pnl >= 0 ? "var(--ta-positive)" : "var(--ta-negative-text)" }}
      >
        {formatSignedEur(trade.realised_eur)} · {formatSigned(trade.realised_r)}R
      </td>
      <td>{trade.exit_reason}</td>
    </tr>
  );
}
