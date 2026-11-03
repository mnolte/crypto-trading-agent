import {
  AreaSeries,
  CandlestickSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";
import { chartPriceFormat } from "../lib/format";

export type Candle = {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
};

/**
 * Price pane built on TradingView Lightweight Charts, themed with the design
 * tokens. Candlesticks or a line series.
 */
export function CandleChart({
  candles,
  mode = "candles",
  height = 300,
}: {
  candles: Candle[];
  mode?: "candles" | "line";
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const style = getComputedStyle(document.documentElement);
    const css = (name: string) => style.getPropertyValue(name).trim();

    const chart: IChartApi = createChart(ref.current, {
      height,
      layout: {
        background: { color: "transparent" },
        textColor: css("--ta-text-muted"),
        fontFamily: css("--ta-font-mono"),
        fontSize: 11,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: css("--ta-chart-grid") },
        horzLines: { color: css("--ta-chart-grid") },
      },
      rightPriceScale: { borderColor: css("--ta-border") },
      timeScale: { borderColor: css("--ta-border"), timeVisible: true },
      crosshair: {
        vertLine: { color: css("--ta-border-strong") },
        horzLine: { color: css("--ta-border-strong") },
      },
    });

    const priceFormat = chartPriceFormat(candles.at(-1)?.close ?? 1);
    let series: ISeriesApi<"Candlestick"> | ISeriesApi<"Line">;
    if (mode === "candles") {
      series = chart.addSeries(CandlestickSeries, {
        upColor: css("--ta-positive"),
        downColor: css("--ta-negative"),
        wickUpColor: css("--ta-positive"),
        wickDownColor: css("--ta-negative"),
        borderVisible: false,
        priceFormat,
      });
      series.setData(candles);
    } else {
      const line = chart.addSeries(LineSeries, {
        color: css("--ta-accent"),
        lineWidth: 2,
        priceFormat,
      });
      line.setData(candles.map((c) => ({ time: c.time, value: c.close })));
      series = line;
    }
    chart.timeScale().fitContent();

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
  }, [candles, mode, height]);

  return <div ref={ref} className="chart-box" style={{ height }} />;
}

export type PriceMarker = {
  time: UTCTimestamp;
  kind: "buy" | "sell" | "decision";
  /** letter shown in the badge, e.g. "B", "S", "D" */
  text: string;
};

export type PriceLine = {
  price: number;
  color: "up" | "down" | "accent" | "muted";
  title: string;
};

/**
 * Dashboard chart: candles plus agent buy/sell/decision markers and the open
 * position's entry/stop/target as horizontal lines.
 */
export function PriceChart({
  candles,
  markers = [],
  lines = [],
  height = 300,
}: {
  candles: Candle[];
  markers?: PriceMarker[];
  lines?: PriceLine[];
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const style = getComputedStyle(document.documentElement);
    const css = (name: string) => style.getPropertyValue(name).trim();

    const chart: IChartApi = createChart(ref.current, {
      height,
      layout: {
        background: { color: "transparent" },
        textColor: css("--ta-text-muted"),
        fontFamily: css("--ta-font-mono"),
        fontSize: 11,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: css("--ta-chart-grid") },
        horzLines: { color: css("--ta-chart-grid") },
      },
      rightPriceScale: { borderColor: css("--ta-border") },
      timeScale: { borderColor: css("--ta-border"), timeVisible: true },
      crosshair: {
        vertLine: { color: css("--ta-border-strong") },
        horzLine: { color: css("--ta-border-strong") },
      },
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: css("--ta-positive"),
      downColor: css("--ta-negative"),
      wickUpColor: css("--ta-positive"),
      wickDownColor: css("--ta-negative"),
      borderVisible: false,
      priceFormat: chartPriceFormat(candles.at(-1)?.close ?? 1),
    });
    series.setData(candles);

    createSeriesMarkers(
      series,
      markers.map((m) => ({
        time: m.time,
        position: m.kind === "buy" ? "belowBar" : "aboveBar",
        color:
          m.kind === "buy"
            ? css("--ta-positive")
            : m.kind === "sell"
              ? css("--ta-negative")
              : css("--ta-accent"),
        shape: "circle",
        text: m.text,
        size: 1,
      })),
    );

    const LINE_COLOR: Record<PriceLine["color"], string> = {
      up: css("--ta-positive"),
      down: css("--ta-negative-text"),
      accent: css("--ta-accent"),
      muted: css("--ta-text-muted"),
    };
    for (const line of lines) {
      series.createPriceLine({
        price: line.price,
        color: LINE_COLOR[line.color],
        lineStyle: LineStyle.Dashed,
        lineWidth: 1,
        title: line.title,
      });
    }

    chart.timeScale().fitContent();

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
  }, [candles, markers, lines, height]);

  return <div ref={ref} className="chart-box" style={{ height }} />;
}

export type LinePoint = { time: UTCTimestamp; value: number };

/**
 * Performance chart: agent equity vs holding the same euros in BTC (pane 0),
 * drawdown from peak with the kill line (pane 1).
 */
export function PerfChart({
  equity,
  benchmark,
  drawdown,
  killPct,
  height = 390,
}: {
  equity: LinePoint[];
  benchmark: LinePoint[];
  drawdown: LinePoint[];
  /** e.g. 8 for "kill at −8%" */
  killPct: number;
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const style = getComputedStyle(document.documentElement);
    const css = (name: string) => style.getPropertyValue(name).trim();

    const chart: IChartApi = createChart(ref.current, {
      height,
      layout: {
        background: { color: "transparent" },
        textColor: css("--ta-text-muted"),
        fontFamily: css("--ta-font-mono"),
        fontSize: 11,
        attributionLogo: false,
        panes: { separatorColor: css("--ta-border"), enableResize: false },
      },
      grid: {
        vertLines: { color: css("--ta-chart-grid") },
        horzLines: { color: css("--ta-chart-grid") },
      },
      rightPriceScale: { borderColor: css("--ta-border") },
      timeScale: { borderColor: css("--ta-border"), timeVisible: true },
      crosshair: {
        vertLine: { color: css("--ta-border-strong") },
        horzLine: { color: css("--ta-border-strong") },
      },
    });

    const eq = chart.addSeries(
      AreaSeries,
      {
        lineColor: css("--ta-accent"),
        lineWidth: 2,
        topColor: "rgba(242, 185, 75, 0.08)",
        bottomColor: "rgba(242, 185, 75, 0.01)",
        priceLineVisible: false,
      },
      0,
    );
    eq.setData(equity);
    chart
      .addSeries(
        LineSeries,
        {
          color: css("--ta-text-muted"),
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          priceLineVisible: false,
          lastValueVisible: false,
        },
        0,
      )
      .setData(benchmark);

    const dd = chart.addSeries(
      AreaSeries,
      {
        lineColor: css("--ta-negative-text"),
        lineWidth: 1,
        topColor: "rgba(255, 99, 105, 0.05)",
        bottomColor: "rgba(255, 99, 105, 0.25)",
        priceLineVisible: false,
      },
      1,
    );
    dd.setData(drawdown);
    dd.createPriceLine({
      price: -killPct,
      color: css("--ta-negative"),
      lineStyle: LineStyle.Dashed,
      lineWidth: 1,
      title: `−${killPct}% kill`,
    });
    chart.panes()[0]?.setStretchFactor(2.6);
    chart.panes()[1]?.setStretchFactor(1);

    chart.timeScale().fitContent();

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
  }, [equity, benchmark, drawdown, killPct, height]);

  return <div ref={ref} className="chart-box" style={{ height }} />;
}

/** Small SVG line+area chart (calibration, sparklines, dashboard preview). */
export function Sparkline({
  points,
  height = 300,
}: {
  /** y values 0..100 */
  points: number[];
  height?: number;
}) {
  const width = 800;
  const n = points.length;
  const coords = points
    .map(
      (y, i) =>
        `${((i / Math.max(1, n - 1)) * width).toFixed(1)},${(height - (y / 100) * height).toFixed(1)}`,
    )
    .join(" ");
  return (
    <div className="chart-box" style={{ height }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        width="100%"
        height={height}
        aria-hidden="true"
        style={{ display: "block" }}
      >
        {[0.25, 0.5, 0.75].map((f) => (
          <line
            key={f}
            x1="0"
            y1={height * f}
            x2={width}
            y2={height * f}
            stroke="var(--ta-chart-grid)"
            strokeWidth="1"
          />
        ))}
        <polygon
          points={`0,${height} ${coords} ${width},${height}`}
          fill="var(--ta-accent)"
          fillOpacity="0.08"
        />
        <polyline
          points={coords}
          fill="none"
          stroke="var(--ta-accent)"
          strokeWidth="2"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
    </div>
  );
}
