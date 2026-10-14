import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { InsightsOut, InsightSliceOut } from "../api/client";
import { useInsights } from "../api/hooks";
import { AppShell } from "../components/AppShell";
import { Card } from "../components/Card";
import { Chip, type ChipVariant } from "../components/Chip";
import { DataTable } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { Select } from "../components/FormField";
import { SegmentedControl } from "../components/SegmentedControl";

/** User feature 9: evidence slices and the calibration chart. All numbers
 * come from the backend; a slice needs 30 decisions to be a finding, shows
 * greyed from 10 and is hidden below that. */
const DIMENSIONS: { value: string; label: string; col: string }[] = [
  { value: "pair", label: "Pair", col: "Pair" },
  { value: "hour", label: "Hour of day", col: "Hour" },
  { value: "weekday", label: "Weekday", col: "Weekday" },
  { value: "regime", label: "Regime", col: "Regime" },
  { value: "volatility", label: "Volatility", col: "Volatility" },
  { value: "confidence", label: "Confidence", col: "Confidence" },
  { value: "decider", label: "Decider", col: "Decider" },
  { value: "prompt_version", label: "Prompt", col: "Prompt version" },
];

const PERIODS = [
  { value: "30d", label: "Last 30 days" },
  { value: "7d", label: "Last 7 days" },
  { value: "all", label: "Since start" },
];

export function Insights() {
  const [dimension, setDimension] = useState("pair");
  const [period, setPeriod] = useState("30d");
  const [decider, setDecider] = useState("all");

  const from = useMemo(() => {
    if (period === "all") return undefined;
    const days = period === "7d" ? 7 : 30;
    return new Date(Date.now() - days * 86_400_000).toISOString();
  }, [period]);

  const { data, isPending } = useInsights({ dimension, from, decider });

  return (
    <AppShell
      title="Insights"
      actions={
        <>
          <label className="topbar-field">
            <span className="topbar-field__label">Period</span>
            <Select
              value={period}
              onChange={(e) => setPeriod(e.target.value)}
            >
              {PERIODS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </Select>
          </label>
          <label className="topbar-field">
            <span className="topbar-field__label">Decider</span>
            <Select
              value={decider}
              onChange={(e) => setDecider(e.target.value)}
            >
              <option value="all">All deciders</option>
              {(data?.decider_options ?? []).map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </Select>
          </label>
        </>
      }
    >
      {data && data.findings.length > 0 && (
        <section aria-label="Findings" className="insights-findings">
          {data.findings.map((f) => (
            <Card key={f.title} className="finding">
              <Chip variant={findingChip(f.kind)}>
                {findingTitle(f.kind)}
              </Chip>
              <div className="finding__title">{f.title}</div>
              <div className="card__meta">
                {f.text} · n = {f.n}
                {f.whatif_hint && (
                  <>
                    {" · "}
                    <Link to="/what-if">{f.whatif_hint}</Link>
                  </>
                )}
              </div>
            </Card>
          ))}
        </section>
      )}

      <div className="insights-layout">
        <Card label="Sliced results" className="insights-slices">
          <SegmentedControl
            flat
            label="Slice by"
            options={DIMENSIONS}
            value={dimension}
            onChange={setDimension}
          />
          {isPending ? (
            <EmptyState title="Loading…" />
          ) : data && data.slices.length > 0 ? (
            <>
              <DataTable
                minWidth={620}
                columns={[
                  {
                    key: "dim",
                    label: DIMENSIONS.find((d) => d.value === dimension)?.col,
                  },
                  { key: "n", label: "Decisions", numeric: true },
                  { key: "buys", label: "BUYs", numeric: true },
                  { key: "win", label: "BUY win rate", numeric: true },
                  { key: "r", label: "Avg R per BUY", numeric: true },
                  { key: "avoided", label: "Avoided", numeric: true },
                  { key: "missed", label: "Missed", numeric: true },
                ]}
              >
                {data.slices.map((s) => (
                  <SliceRow
                    key={s.key}
                    slice={s}
                    maxR={Math.max(
                      1,
                      ...data.slices.map((x) => Math.abs(x.avg_r ?? 0)),
                    )}
                  />
                ))}
              </DataTable>
              <p className="card__meta">
                Rows with fewer than {data.min_decisions} decisions are greyed:
                too few to trust.
                {data.decisions_needed > 0 &&
                  ` The largest slice (${data.largest_slice_label}) has ${data.largest_slice_decisions} of ${data.min_decisions} — ${data.decisions_needed} more to go.`}
                {" "}Avoided: the hypothetical stop was hit first after a
                HOLD or SKIP. Missed: the target was hit first.
              </p>
            </>
          ) : (
            <EmptyState
              title="No findings yet"
              footer={
                data && data.decisions_needed > 0
                  ? `At least ${data.decisions_needed} more decisions to go.`
                  : undefined
              }
            >
              {data && (
                <>
                  {`A finding needs ${data.min_decisions} decisions in a slice. You have ${data.total_decisions} so far${
                    data.largest_slice_label
                      ? `; the largest slice (${data.largest_slice_label}) has ${data.largest_slice_decisions}`
                      : ""
                  }. Early signals appear greyed from ${data.slice_min}.`}
                  {data.largest_slice_decisions > 0 && (
                    <div
                      className="meter meter--muted"
                      style={{ marginTop: "var(--space-2)" }}
                      role="progressbar"
                      aria-valuemin={0}
                      aria-valuemax={data.min_decisions}
                      aria-valuenow={data.largest_slice_decisions}
                    >
                      <div
                        className="meter__fill"
                        style={{
                          width: `${Math.min(100, (data.largest_slice_decisions / data.min_decisions) * 100)}%`,
                        }}
                      />
                    </div>
                  )}
                </>
              )}
            </EmptyState>
          )}
        </Card>

        <Card
          label="Calibration"
          className="insights-cal"
          title="Calibration"
          heading="section"
        >
          <p className="card__meta">
            Does the model's confidence match reality? Bars show the actual
            BUY win rate per confidence band; the line is where they should
            be.
          </p>
          {data && <Calibration data={data} />}
          {data?.calibration_gap_pts != null && (
            <div className="calibration__gap">
              Calibration gap:{" "}
              <span className="num down">
                {data.calibration_gap_pts.toFixed(0)} pts
              </span>{" "}
              above 0.7.
            </div>
          )}
        </Card>
      </div>
    </AppShell>
  );
}

function findingTitle(kind: string) {
  switch (kind) {
    case "overconfident":
      return "Overconfident";
    case "underconfident":
      return "Underconfident";
    case "weak_spot":
      return "Weak spot";
    default:
      return "Strength";
  }
}

function findingChip(kind: string): ChipVariant {
  switch (kind) {
    case "overconfident":
    case "underconfident":
      return "down";
    case "weak_spot":
      return "amber";
    default:
      return "up";
  }
}

function SliceRow({
  slice,
  maxR,
}: {
  slice: InsightSliceOut;
  maxR: number;
}) {
  const early = slice.state === "early";
  const r = slice.avg_r;
  const half = r != null ? Math.min(Math.abs(r) / maxR, 1) * 50 : 0;
  return (
    <tr className={early ? "row--early" : undefined}>
      <td>
        {slice.label}
        {early && <span className="early-tag">too few</span>}
      </td>
      <td className="num">{slice.decisions}</td>
      <td className="num">{slice.buys}</td>
      <td className="num">
        {slice.buys > 0 && slice.buy_win_rate_pct != null
          ? `${slice.buy_win_rate_pct.toFixed(0)}%`
          : "—"}
      </td>
      <td>
        <div className="rbar-cell">
          <div className="rbar">
            <i className="rbar__zero" />
            {r != null && (
              <i
                className={`rbar__fill ${r >= 0 ? "rbar__fill--up" : "rbar__fill--down"}`}
                style={{
                  left: r >= 0 ? "50%" : `${50 - half}%`,
                  width: `${half}%`,
                }}
              />
            )}
          </div>
          <span className={`num ${r != null ? (r >= 0 ? "up" : "down") : ""}`}>
            {r != null ? `${r >= 0 ? "+" : ""}${r.toFixed(2)}` : "—"}
          </span>
        </div>
      </td>
      <td className="num">
        {slice.avoided_pct != null ? `${slice.avoided_pct.toFixed(0)}%` : "—"}
      </td>
      <td className="num">
        {slice.missed_pct != null ? `${slice.missed_pct.toFixed(0)}%` : "—"}
      </td>
    </tr>
  );
}

const CAL_W = 360;
const CAL_H = 240;
const PLOT = { left: 40, right: 350, top: 20, bottom: 200 };

function Calibration({ data }: { data: InsightsOut }) {
  const bands = data.calibration;
  const y = (pct: number) =>
    PLOT.bottom - (pct / 100) * (PLOT.bottom - PLOT.top);
  const bw = (PLOT.right - PLOT.left) / Math.max(bands.length, 1);
  const barW = Math.min(52, bw * 0.7);
  const cx = (i: number) => PLOT.left + bw * i + bw / 2;
  const statedPts = bands
    .filter((b) => b.stated_pct != null)
    .map((b) => `${cx(bands.indexOf(b))},${y(b.stated_pct ?? 0)}`)
    .join(" ");
  return (
    <div className="calibration">
      <svg
        viewBox={`0 0 ${CAL_W} ${CAL_H}`}
        width="100%"
        role="img"
        aria-label="Calibration: actual BUY win rate against stated confidence"
      >
        {[0, 50, 100].map((p) => (
          <g key={p}>
            <line
              x1={PLOT.left}
              y1={y(p)}
              x2={PLOT.right}
              y2={y(p)}
              stroke={p === 0 ? "var(--line-strong)" : "var(--series-grid)"}
            />
            <text
              x={PLOT.left - 6}
              y={y(p) + 4}
              fill="var(--text-muted)"
              fontSize="11"
              textAnchor="end"
              fontFamily="IBM Plex Mono, monospace"
            >
              {p}%
            </text>
          </g>
        ))}
        {bands.map((b, i) => {
          const actual = b.actual_pct;
          if (actual == null) return null;
          const off = (b.stated_pct ?? 0) - actual;
          const offColor =
            off > 10 ? "var(--down-text)" : "var(--series-ema50)";
          const top = y(actual);
          return (
            <rect
              key={b.label}
              x={cx(i) - barW / 2}
              y={top}
              width={barW}
              height={PLOT.bottom - top}
              rx="3"
              fill={offColor}
              fillOpacity="0.8"
            />
          );
        })}
        {statedPts && (
          <polyline
            points={statedPts}
            fill="none"
            stroke="var(--accent)"
            strokeWidth="2"
            strokeDasharray="5 4"
          />
        )}
        {bands.map((b, i) =>
          b.stated_pct != null ? (
            <circle
              key={b.label}
              cx={cx(i)}
              cy={y(b.stated_pct)}
              r="3"
              fill="var(--accent)"
            />
          ) : null,
        )}
        {bands.map((b, i) => (
          <g key={b.label}>
            <text
              x={cx(i)}
              y={PLOT.bottom + 18}
              fill="var(--text-muted)"
              fontSize="11"
              textAnchor="middle"
              fontFamily="IBM Plex Mono, monospace"
            >
              {b.label}
            </text>
            <text
              x={cx(i)}
              y={PLOT.bottom + 34}
              fill="var(--text-muted)"
              fontSize="10"
              textAnchor="middle"
              fontFamily="IBM Plex Mono, monospace"
            >
              n={b.buys}
            </text>
          </g>
        ))}
      </svg>
      <div className="chart-legend">
        <span className="chart-legend__item">
          <i className="calibration__line" /> Stated confidence
        </span>
        <span className="chart-legend__item">
          <i className="chart-marker calibration__ok" /> Actual, close enough
        </span>
        <span className="chart-legend__item">
          <i className="chart-marker calibration__off" /> Actual, more than 10
          pts below
        </span>
      </div>
    </div>
  );
}
