import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import type { UTCTimestamp } from "lightweight-charts";
import type { IndicatorRowOut, Tf } from "../api/client";
import { useDecisions, useIndicators, useSettings, useSnapshot } from "../api/hooks";
import { AppShell } from "../components/AppShell";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import type { Candle } from "../components/Charts";
import { Chip } from "../components/Chip";
import { DataTable } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import {
  IndicatorChart,
  type IndicatorSeries,
  type LevelLine,
  type RegimeBand,
  type Series,
} from "../components/IndicatorChart";
import type { PriceMarker } from "../components/Charts";
import { PairTabs, type PairTabItem } from "../components/PairTabs";
import { RegimeChip } from "../components/RegimeChip";
import { SegmentedControl } from "../components/SegmentedControl";
import {
  formatAge,
  formatChange,
  formatEurCompact,
  formatPct,
  formatPrice,
  formatSigned,
  formatTime,
} from "../lib/format";

const TFS: Tf[] = ["1h", "4h", "1d"];
const SERIES_KEYS = [
  "ema20",
  "ema50",
  "ema200",
  "vwap",
  "rsi14",
  "adx14",
  "atr_pct",
  "obv",
  "vol_avg",
] as const;

const READ_COLOR: Record<string, string> = {
  bullish: "var(--ta-positive)",
  trending: "var(--ta-positive)",
  bearish: "var(--ta-negative-text)",
  high: "var(--ta-accent)",
  neutral: "var(--ta-text-2)",
  ranging: "var(--ta-text-2)",
  low: "var(--ta-text-2)",
  normal: "var(--ta-text-2)",
};

export function Indicators() {
  const { data: snapshot } = useSnapshot();
  const { data: settings } = useSettings();
  const [params, setParams] = useSearchParams();
  const [tf, setTf] = useState<Tf>("1h");
  const [overlays, setOverlays] = useState({ ema: true, vwap: true, levels: true });
  const [hover, setHover] = useState<{ time: string; rows: [string, string][] } | null>(null);

  const markets = settings?.active_markets ?? [];
  const selected = params.get("pair") ?? markets[0] ?? null;
  const at = params.get("at");
  const { data } = useIndicators(selected, tf, at);
  const { data: latestDecision } = useDecisions(selected ?? undefined, 1);
  const lastDecisionId = latestDecision?.items[0]?.id;

  const setPair = (pair: string) =>
    setParams((p) => {
      p.set("pair", pair);
      p.delete("at");
      return p;
    });

  const pairTabs: PairTabItem[] = markets.map((m) => ({
    id: m,
    chg24h: Number(snapshot?.prices[m]?.chg_24h_pct ?? 0),
    stale: snapshot?.prices[m]?.stale ?? false,
  }));

  const candles: Candle[] = useMemo(
    () =>
      (data?.candles ?? []).map((c) => ({
        time: (new Date(c.time).getTime() / 1000) as UTCTimestamp,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
        volume: c.volume,
      })),
    [data],
  );
  const series = useMemo(() => {
    const out = {} as IndicatorSeries;
    for (const key of SERIES_KEYS) {
      out[key] = (data?.series[key] ?? []).map((p) => ({
        time: (new Date(p.time).getTime() / 1000) as UTCTimestamp,
        value: p.value,
      })) as Series;
    }
    return out;
  }, [data]);
  const regimeBand: RegimeBand = useMemo(
    () =>
      (data?.regime_band ?? []).map((p) => ({
        time: (new Date(p.time).getTime() / 1000) as UTCTimestamp,
        value: p.value,
      })),
    [data],
  );
  const markers: PriceMarker[] = (data?.markers ?? []).map((m) => ({
    time: (new Date(m.time).getTime() / 1000) as UTCTimestamp,
    kind: m.kind,
    text: m.label || (m.kind === "buy" ? "B" : m.kind === "sell" ? "S" : "D"),
  }));
  const levels: LevelLine[] = (data?.levels ?? []).map((l) => ({
    price: l.price,
    kind:
      l.kind === "prev_day_high" || l.kind === "prev_day_low"
        ? "prev_day"
        : l.kind,
  }));

  const last = data?.candles.at(-1)?.close;

  return (
    <AppShell title="Indicators">
      <div className="chart-head">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
          <PairTabs pairs={pairTabs} value={selected ?? ""} onChange={setPair} />
          <SegmentedControl
            label="Timeframe"
            value={tf}
            onChange={(v) => setTf(v as Tf)}
            options={TFS.map((t) => ({ value: t, label: t }))}
          />
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
          <span className="card__meta">Overlays</span>
          <OverlayToggle
            label="EMA 20 / 50 / 200"
            on={overlays.ema}
            onClick={() => setOverlays((o) => ({ ...o, ema: !o.ema }))}
          />
          <OverlayToggle
            label="VWAP"
            on={overlays.vwap}
            onClick={() => setOverlays((o) => ({ ...o, vwap: !o.vwap }))}
          />
          <OverlayToggle
            label="Levels"
            on={overlays.levels}
            onClick={() => setOverlays((o) => ({ ...o, levels: !o.levels }))}
          />
          <Button
            variant="secondary"
            size="sm"
            disabled={!lastDecisionId}
            title={
              lastDecisionId
                ? "Recompute every value from the candles that were closed at the last decision"
                : "No decision for this pair yet"
            }
            onClick={() =>
              lastDecisionId &&
              setParams((p) => {
                p.set("at", lastDecisionId);
                return p;
              })
            }
          >
            Last decision view
          </Button>
          {at && (
            <Button
              variant="secondary"
              size="sm"
              onClick={() =>
                setParams((p) => {
                  p.delete("at");
                  return p;
                })
              }
            >
              Back to live
            </Button>
          )}
          <a
            className="btn btn--secondary btn--sm"
            href={`/api/indicators/${selected}/export.csv?tf=${tf}`}
            download
          >
            Export CSV
          </a>
        </div>
      </div>

      {data?.frozen && (
        <div className="frozen-bar" role="status">
          Frozen at the decision from{" "}
          {data.frozen_at ? formatTime(data.frozen_at) : "—"}
          {data.frozen_cycle !== null ? ` (cycle #${data.frozen_cycle})` : ""} —
          every value uses the candles closed at that moment.
          <Button
            variant="secondary"
            size="sm"
            onClick={() =>
              setParams((p) => {
                p.delete("at");
                return p;
              })
            }
          >
            Back to live
          </Button>
        </div>
      )}

      <div className="row">
        <Card className="row__main" label="Charts">
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: 12 }}>
            <div style={{ fontWeight: 600, fontSize: 16 }}>
              {selected} · {tf}
            </div>
            <div className="num" style={{ fontSize: 22 }}>
              {last !== undefined ? formatPrice(last) : "—"}
            </div>
            <div className="card__meta">
              {data ? `${data.candles.length} of ${data.health.bars_requested} bars` : ""}
              {data?.health.stale ? " · stale" : ""}
            </div>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, fontSize: 12, color: "var(--ta-text-muted)" }}>
            <span>Regime</span>
            <RegimeChip
              regime={data?.regime ?? null}
              label={selected ?? undefined}
            />
            {data?.btc_regime && selected !== "BTC-EUR" && (
              <RegimeChip regime={data.btc_regime} label="BTC" />
            )}
          </div>
          {candles.length > 0 ? (
            <IndicatorChart
              candles={candles}
              series={series}
              regimeBand={regimeBand}
              markers={markers}
              levels={levels}
              overlays={overlays}
              adxThreshold={data?.adx_threshold ?? 25}
              onHover={setHover}
            />
          ) : (
            <EmptyState
              title="No candles yet"
              footer="Candles load on the first cycle."
            />
          )}
          <div className="chart-legend">
            <span style={{ color: "var(--ta-chart-ema20)" }}>EMA 20</span>
            <span style={{ color: "var(--ta-chart-ema50)" }}>EMA 50</span>
            <span style={{ color: "var(--ta-chart-ema200)" }}>EMA 200</span>
            <span style={{ color: "var(--ta-chart-vwap)" }}>VWAP</span>
            <span>dashed: levels · dotted: prev. day H/L</span>
          </div>
          {hover && (
            <div className="kv-box num" style={{ fontSize: 12 }}>
              <div className="kv">
                <span>bar</span>
                <span>{formatTime(new Date(Number(hover.time) * 1000).toISOString())}</span>
              </div>
              {hover.rows.map(([k, v]) => (
                <div className="kv" key={k}>
                  <span>{k}</span>
                  <span>{v}</span>
                </div>
              ))}
            </div>
          )}
          <div className="card__meta">
            Hover any bar to read every value at that bar.
          </div>
        </Card>

        <div className="row__side" style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <Card title="Current values" label="Current values">
            {data && data.current.length > 0 ? (
              <CurrentTable rows={data.current} />
            ) : (
              <EmptyState title="No data" footer="Values appear after the first cycle." />
            )}
            <p className="card__meta" style={{ margin: 0, lineHeight: 1.5 }}>
              Reads: RSI above 55 bullish, below 45 bearish · VWAP and EMA 200
              distance beyond ±0.1% · OBV trend beyond ±0.1 · EMA 20 vs 50
              crossover · ADX {data?.adx_threshold ?? 25} or more trending.
            </p>
          </Card>

          <Card title="Structure &amp; levels" label="Structure and levels">
            {data ? (
              <>
                {data.structure.length > 0 && (
                  <ul className="structure-list">
                    {data.structure.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                )}
                <ul className="structure-list">
                  {data.levels.map((l) => (
                    <li
                      key={`${l.kind}-${l.price}`}
                      style={{
                        display: "flex",
                        gap: 8,
                        padding: "7px 0",
                        borderTop: "1px solid var(--ta-border)",
                      }}
                    >
                      <Chip
                        variant={
                          l.kind === "resistance"
                            ? "down"
                            : l.kind === "support"
                              ? "up"
                              : "neutral"
                        }
                      >
                        {l.kind === "prev_day_high"
                          ? "prev day high"
                          : l.kind === "prev_day_low"
                            ? "prev day low"
                            : l.kind}
                      </Chip>
                      <span className="num" style={{ flex: 1 }}>
                        {formatPrice(l.price)}
                      </span>
                      <span className="num" style={{ color: "var(--ta-text-muted)" }}>
                        {l.dist_pct !== null ? formatChange(l.dist_pct, 1) : "—"}
                      </span>
                    </li>
                  ))}
                </ul>
                <div className="kv-box">
                  <div className="kv">
                    <span>vs BTC 24h</span>
                    <span className="num">
                      {data.context.rs_btc_24h_pct !== null &&
                      data.context.rs_btc_24h_pct !== undefined
                        ? formatChange(data.context.rs_btc_24h_pct, 1)
                        : "—"}
                    </span>
                  </div>
                  <div className="kv">
                    <span>BTC correlation 30d</span>
                    <span className="num">
                      {data.context.corr_btc_30d !== null &&
                      data.context.corr_btc_30d !== undefined
                        ? formatSigned(data.context.corr_btc_30d)
                        : "—"}
                    </span>
                  </div>
                  <div className="kv">
                    <span>Spread</span>
                    <span className="num">
                      {data.liquidity.spread_pct !== null
                        ? formatPct(data.liquidity.spread_pct, 2)
                        : "—"}
                    </span>
                  </div>
                  <div className="kv">
                    <span>Depth ±0.5%</span>
                    <span className="num">
                      {data.liquidity.depth_eur !== null
                        ? formatEurCompact(data.liquidity.depth_eur)
                        : "—"}
                    </span>
                  </div>
                  <div className="kv">
                    <span>24h volume vs 30 d</span>
                    <span className="num">
                      {data.liquidity.vol24h_vs_30d !== null
                        ? `${data.liquidity.vol24h_vs_30d.toFixed(2)}×`
                        : "—"}
                    </span>
                  </div>
                </div>
              </>
            ) : (
              <EmptyState title="No data" />
            )}
          </Card>

          <Card title="Data health" label="Data health">
            {data ? (
              <div className="kv-box" style={{ background: "transparent", border: 0, padding: 0 }}>
                <div className="kv">
                  <span>Bars loaded</span>
                  <span className="num">
                    {data.health.bars_loaded} / {data.health.bars_requested}
                  </span>
                </div>
                <div className="kv">
                  <span>Newest closed candle</span>
                  <span
                    className="num"
                    style={{
                      color: data.health.stale ? "var(--ta-accent)" : "var(--ta-positive)",
                    }}
                  >
                    {data.health.newest_closed_age_s !== null
                      ? `${formatAge(data.health.newest_closed_age_s)} old`
                      : "—"}
                  </span>
                </div>
                <div className="kv">
                  <span>Missing bars</span>
                  <span
                    className="num"
                    style={{
                      color:
                        data.health.missing_bars > 0
                          ? "var(--ta-accent)"
                          : "var(--ta-positive)",
                    }}
                  >
                    {data.health.missing_bars}
                  </span>
                </div>
                <div className="kv">
                  <span>Source</span>
                  <span>{data.health.source}</span>
                </div>
              </div>
            ) : (
              <EmptyState title="No data" />
            )}
          </Card>
        </div>
      </div>
    </AppShell>
  );
}

function OverlayToggle({
  label,
  on,
  onClick,
}: {
  label: string;
  on: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      className={on ? "chip chip--pill" : "chip chip--pill chip--neutral"}
      style={
        on
          ? {
              borderColor: "var(--ta-accent)",
              background: "var(--ta-accent-bg)",
              color: "var(--ta-text)",
            }
          : undefined
      }
      onClick={onClick}
    >
      {label}
    </button>
  );
}

function CurrentTable({ rows }: { rows: IndicatorRowOut[] }) {
  return (
    <DataTable
      columns={[
        { key: "name", label: "Indicator" },
        ...TFS.map((tf) => ({ key: tf, label: tf, numeric: true })),
      ]}
    >
      {rows.map((row) => (
        <tr key={row.name} title={row.threshold}>
          <td className="text">{row.name}</td>
          {TFS.map((tf) => {
            const cell = row.cells[tf];
            return (
              <td
                key={tf}
                className="num"
                style={{ color: cell?.read ? READ_COLOR[cell.read] : undefined }}
                title={row.threshold}
              >
                {cell?.text ?? "—"}
              </td>
            );
          })}
        </tr>
      ))}
    </DataTable>
  );
}
