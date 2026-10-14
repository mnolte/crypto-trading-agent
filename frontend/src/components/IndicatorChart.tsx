import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { Candle } from "./Charts";
import type { PriceMarker } from "./Charts";

export type Series = { time: UTCTimestamp; value: number | null }[];
export type RegimeBand = { time: UTCTimestamp; value: "up" | "down" | "sideways" }[];
export type LevelLine = { price: number; kind: "support" | "resistance" | "prev_day" };

export type IndicatorSeries = {
  ema20: Series;
  ema50: Series;
  ema200: Series;
  vwap: Series;
  rsi14: Series;
  adx14: Series;
  atr_pct: Series;
  obv: Series;
  vol_avg: Series;
};

type Hover = { time: string; rows: [string, string][] };

/**
 * The Indicators screen's stacked panes on one shared time axis: price with
 * overlays, a per-bar regime band, then RSI, ADX, OBV, ATR% and volume.
 * Hovering shows every series value at that bar.
 */
export function IndicatorChart({
  candles,
  series,
  regimeBand,
  markers = [],
  levels = [],
  overlays = { ema: true, vwap: true, levels: true },
  adxThreshold = 25,
  height = 780,
  onHover,
}: {
  candles: Candle[];
  series: IndicatorSeries;
  regimeBand: RegimeBand;
  markers?: PriceMarker[];
  levels?: LevelLine[];
  overlays?: { ema: boolean; vwap: boolean; levels: boolean };
  adxThreshold?: number;
  height?: number;
  onHover?: (hover: Hover | null) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const hoverRef = useRef(onHover);
  hoverRef.current = onHover;

  useEffect(() => {
    if (!ref.current) return;
    const style = getComputedStyle(document.documentElement);
    const css = (name: string) => style.getPropertyValue(name).trim();

    const chart: IChartApi = createChart(ref.current, {
      height,
      layout: {
        background: { color: "transparent" },
        textColor: css("--text-muted"),
        fontFamily: css("--font-num"),
        fontSize: 11,
        attributionLogo: false,
        panes: { separatorColor: css("--line"), enableResize: false },
      },
      grid: {
        vertLines: { color: css("--series-grid") },
        horzLines: { color: css("--series-grid") },
      },
      rightPriceScale: { borderColor: css("--line") },
      timeScale: { borderColor: css("--line"), timeVisible: true },
      crosshair: {
        vertLine: { color: css("--line-strong") },
        horzLine: { color: css("--line-strong") },
      },
    });

    const toLine = (pts: Series) =>
      pts.flatMap((p) => (p.value === null ? [] : [{ time: p.time, value: p.value }]));

    // Pane 0: price
    const price = chart.addSeries(CandlestickSeries, {
      upColor: css("--up"),
      downColor: css("--down"),
      wickUpColor: css("--up"),
      wickDownColor: css("--down"),
      borderVisible: false,
    });
    price.setData(candles);
    createSeriesMarkers(
      price,
      markers.map((m) => ({
        time: m.time,
        position: m.kind === "buy" ? "belowBar" : "aboveBar",
        color:
          m.kind === "buy" ? css("--up") : m.kind === "sell" ? css("--down") : css("--accent"),
        shape: "circle",
        text: m.text,
        size: 1,
      })),
    );

    if (overlays.ema) {
      for (const [key, color, width] of [
        ["ema20", css("--series-ema20"), 1],
        ["ema50", css("--series-ema50"), 1],
        ["ema200", css("--series-ema200"), 1],
      ] as const) {
        chart
          .addSeries(
            LineSeries,
            { color, lineWidth: width, priceLineVisible: false, lastValueVisible: false },
            0,
          )
          .setData(toLine(series[key]));
      }
    }
    if (overlays.vwap) {
      chart
        .addSeries(
          LineSeries,
          { color: css("--series-vwap"), lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false },
          0,
        )
        .setData(toLine(series.vwap));
    }
    if (overlays.levels) {
      for (const lvl of levels) {
        price.createPriceLine({
          price: lvl.price,
          color:
            lvl.kind === "support"
              ? css("--up")
              : lvl.kind === "resistance"
                ? css("--down-text")
                : css("--text-muted"),
          lineStyle: lvl.kind === "prev_day" ? LineStyle.SparseDotted : LineStyle.Dashed,
          lineWidth: 1,
        });
      }
    }

    // Pane 1: the regime band — one bar per candle, coloured by its trend.
    const band = chart.addSeries(
      HistogramSeries,
      { priceLineVisible: false, lastValueVisible: false, priceFormat: { type: "volume" } },
      1,
    );
    const REGIME_COLOR: Record<string, string> = {
      up: css("--up"),
      down: css("--down-text"),
      sideways: css("--side"),
    };
    band.setData(
      regimeBand.map((b) => ({
        time: b.time,
        value: 1,
        color: REGIME_COLOR[b.value] ?? css("--text-muted"),
      })),
    );

    // Panes 2..6: RSI, ADX, OBV, ATR%, volume.
    const rsi = chart.addSeries(
      LineSeries,
      { color: css("--accent"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false },
      2,
    );
    rsi.setData(toLine(series.rsi14));
    for (const p of [45, 55]) {
      rsi.createPriceLine({
        price: p,
        color: css("--line-strong"),
        lineStyle: LineStyle.Dashed,
        lineWidth: 1,
      });
    }

    const adxS = chart.addSeries(
      LineSeries,
      { color: "#5AA9FF", lineWidth: 2, priceLineVisible: false, lastValueVisible: false },
      3,
    );
    adxS.setData(toLine(series.adx14));
    adxS.createPriceLine({
      price: adxThreshold,
      color: css("--line-strong"),
      lineStyle: LineStyle.Dashed,
      lineWidth: 1,
    });

    chart
      .addSeries(
        LineSeries,
        { color: "#B48CFF", lineWidth: 2, priceLineVisible: false, lastValueVisible: false },
        4,
      )
      .setData(toLine(series.obv));

    chart
      .addSeries(
        LineSeries,
        { color: css("--text-soft"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false },
        5,
      )
      .setData(toLine(series.atr_pct));

    const volume = chart.addSeries(
      HistogramSeries,
      { priceLineVisible: false, lastValueVisible: false, priceFormat: { type: "volume" } },
      6,
    );
    volume.setData(
      candles.map((c) => ({
        time: c.time,
        value: c.volume ?? 0,
        color: c.close >= c.open ? "rgba(61, 214, 140, 0.45)" : "rgba(255, 99, 105, 0.45)",
      })),
    );
    chart
      .addSeries(
        LineSeries,
        { color: css("--text"), lineWidth: 1, priceLineVisible: false, lastValueVisible: false },
        6,
      )
      .setData(toLine(series.vol_avg));

    const stretch = [1.6, 0.16, 0.5, 0.42, 0.42, 0.42, 0.42];
    chart.panes().forEach((pane, i) => pane.setStretchFactor(stretch[i] ?? 0.4));

    chart.timeScale().fitContent();

    const lookups: [string, Map<UTCTimestamp, number | null>][] = [
      ["RSI", new Map(series.rsi14.map((p) => [p.time, p.value]))],
      ["ADX", new Map(series.adx14.map((p) => [p.time, p.value]))],
      ["OBV", new Map(series.obv.map((p) => [p.time, p.value]))],
      ["ATR %", new Map(series.atr_pct.map((p) => [p.time, p.value]))],
      ["EMA 20", new Map(series.ema20.map((p) => [p.time, p.value]))],
      ["EMA 50", new Map(series.ema50.map((p) => [p.time, p.value]))],
      ["EMA 200", new Map(series.ema200.map((p) => [p.time, p.value]))],
      ["VWAP", new Map(series.vwap.map((p) => [p.time, p.value]))],
      ["Vol avg", new Map(series.vol_avg.map((p) => [p.time, p.value]))],
    ];
    const candleByTime = new Map(candles.map((c) => [c.time, c]));
    chart.subscribeCrosshairMove((param) => {
      const cb = hoverRef.current;
      if (!cb) return;
      const time = param.time;
      if (!time || !param.point) {
        cb(null);
        return;
      }
      const candle = candleByTime.get(time as UTCTimestamp);
      const rows: [string, string][] = candle
        ? [
            ["O", candle.open.toFixed(4)],
            ["H", candle.high.toFixed(4)],
            ["L", candle.low.toFixed(4)],
            ["C", candle.close.toFixed(4)],
          ]
        : [];
      for (const [name, pts] of lookups) {
        const value = pts.get(time as UTCTimestamp);
        if (value !== undefined && value !== null) rows.push([name, value.toFixed(4)]);
      }
      cb({ time: String(time), rows });
    });

    const observer = new ResizeObserver(() => {
      if (ref.current) {
        chart.applyOptions({ width: ref.current.clientWidth });
      }
    });
    observer.observe(ref.current);

    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, [candles, series, regimeBand, markers, levels, overlays, adxThreshold, height]);

  return <div ref={ref} className="chart-box" style={{ height }} />;
}
