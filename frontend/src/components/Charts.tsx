import {
  CandlestickSeries,
  LineSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

export type Candle = {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
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
        textColor: css("--text-muted"),
        fontFamily: css("--font-num"),
        fontSize: 11,
        attributionLogo: false,
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

    let series: ISeriesApi<"Candlestick"> | ISeriesApi<"Line">;
    if (mode === "candles") {
      series = chart.addSeries(CandlestickSeries, {
        upColor: css("--up"),
        downColor: css("--down"),
        wickUpColor: css("--up"),
        wickDownColor: css("--down"),
        borderVisible: false,
      });
      series.setData(candles);
    } else {
      const line = chart.addSeries(LineSeries, {
        color: css("--accent"),
        lineWidth: 2,
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
            stroke="var(--series-grid)"
            strokeWidth="1"
          />
        ))}
        <polygon
          points={`0,${height} ${coords} ${width},${height}`}
          fill="var(--accent)"
          fillOpacity="0.08"
        />
        <polyline
          points={coords}
          fill="none"
          stroke="var(--accent)"
          strokeWidth="2"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
    </div>
  );
}
