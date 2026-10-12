import { useEffect, useRef, useState } from "react";
import { createChart, LineSeries, type IChartApi } from "lightweight-charts";
import type { WhatIfSpecIn } from "../api/client";
import {
  useApplyWhatIf,
  useRunWhatIf,
  useSaveWhatIf,
  useSettings,
  useWhatIf,
  useWhatIfList,
} from "../api/hooks";
import { AppShell } from "../components/AppShell";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { DataTable } from "../components/DataTable";
import { Dialog } from "../components/Dialog";
import { EmptyState } from "../components/EmptyState";
import { CheckField, FormField, Input, Select } from "../components/FormField";
import { formatDateTime } from "../lib/format";

/** User feature 8: re-run a stored window under changed settings. The model's
 * answers are held fixed; rules, gates, sizing and fills are recomputed. */

const PERIODS = [
  { value: "30d", label: "Last 30 days" },
  { value: "7d", label: "Last 7 days" },
  { value: "all", label: "Since start" },
];

interface FieldDef {
  key: string; // dotted settings path
  label: string;
  now?: string; // current setting label from live settings
}

export function WhatIf() {
  const { data: settings } = useSettings();
  const { data: savedList } = useWhatIfList();
  const [period, setPeriod] = useState("30d");
  const [weekend, setWeekend] = useState(false);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [jobId, setJobId] = useState<string | null>(null);
  const [applyOpen, setApplyOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: job } = useWhatIf(jobId);
  const run = useRunWhatIf();
  const save = useSaveWhatIf();
  const apply = useApplyWhatIf();

  const fieldDefs: FieldDef[] = [
    {
      key: "risk.min_rr",
      label: "Min. reward / risk",
      now: settings?.risk.min_rr,
    },
    {
      key: "risk.risk_per_trade_pct",
      label: "Risk per trade",
      now: settings?.risk.risk_per_trade_pct,
    },
    {
      key: "risk.time_stop_days",
      label: "Max hold",
      now: settings?.risk.time_stop_days,
    },
    {
      key: "risk.max_cost_pct_of_risk",
      label: "Max costs",
      now: settings?.risk.max_cost_pct_of_risk,
    },
    {
      key: "risk.total_open_risk_pct",
      label: "Total open risk",
      now: settings?.risk.total_open_risk_pct,
    },
  ];

  const startRun = () => {
    setError(null);
    const days = period === "7d" ? 7 : period === "30d" ? 30 : 365;
    const overrides: Record<string, unknown> = {};
    for (const def of fieldDefs) {
      const raw = (fields[def.key] ?? "").trim();
      if (raw !== "") overrides[def.key] = Number(raw);
    }
    if (weekend) overrides["no_weekend_entries"] = true;
    const spec = {
      from: new Date(Date.now() - days * 86_400_000).toISOString(),
      to: new Date().toISOString(),
      overrides,
    };
    run.mutate(spec as WhatIfSpecIn, {
      onSuccess: (j) => setJobId(j.id),
      onError: (e) => setError(e instanceof Error ? e.message : String(e)),
    });
  };

  const running = job?.status === "queued" || job?.status === "running";
  const done = job?.status === "done" && job.result;

  return (
    <AppShell
      title="What-if"
      actions={
        <span className="card__meta">
          Re-runs stored history with different settings
        </span>
      }
    >
      <div className="whatif-layout">
        <Card title="Scenario" heading="section" className="whatif-form">
          <FormField label="Period" htmlFor="wi-period">
            <Select
              id="wi-period"
              value={period}
              onChange={(e) => setPeriod(e.target.value)}
            >
              {PERIODS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </Select>
          </FormField>
          <CheckField
            label="No new entries on Saturday and Sunday"
            checked={weekend}
            onChange={(e) => setWeekend(e.target.checked)}
          />
          <div className="whatif-fields">
            {fieldDefs.map((def) => (
              <FormField
                key={def.key}
                label={def.label + (def.now ? ` · now ${def.now}` : "")}
              >
                <Input
                  mono
                  inputMode="decimal"
                  value={fields[def.key] ?? ""}
                  placeholder={def.now ?? ""}
                  onChange={(e) =>
                    setFields((f) => ({ ...f, [def.key]: e.target.value }))
                  }
                />
              </FormField>
            ))}
          </div>
          <div className="whatif-actions">
            <Button
              variant="primary"
              disabled={run.isPending || running}
              onClick={startRun}
            >
              {running ? "Running…" : "Run"}
            </Button>
            <Button
              disabled={!done}
              onClick={() =>
                jobId &&
                save.mutate({ id: jobId }, { onError: (e) => setError(String(e)) })
              }
            >
              Save scenario
            </Button>
          </div>
          <Button
            disabled={!done}
            className="whatif-apply"
            onClick={() => setApplyOpen(true)}
          >
            Apply these settings…
          </Button>
          {savedList && savedList.items.length > 0 && (
            <div className="whatif-saved">
              <span className="field__label">Saved scenarios</span>
              {savedList.items.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  className="whatif-saved__item"
                  onClick={() => setJobId(s.id)}
                >
                  {s.name ?? s.id} · {s.status}
                </button>
              ))}
            </div>
          )}
        </Card>

        <div className="whatif-results">
          <Banner variant="info">
            The model's answers are held fixed: the simulation reuses what it
            decided at the time and only recomputes the rules, risk gates,
            sizing and fills. It cannot know what the model would have said
            with different inputs.
          </Banner>
          {error && <Banner variant="danger">{error}</Banner>}
          {running && job && (
            <Card>
              <EmptyState
                title={`Running the scenario… ${(job.progress * 100).toFixed(0)}%`}
              />
            </Card>
          )}
          {done && job?.result && (
            <>
              <Card label="Metrics">
                <DataTable
                  minWidth={520}
                  columns={[
                    { key: "label", label: "Metric" },
                    { key: "a", label: "What happened", numeric: true },
                    { key: "s", label: "Scenario", numeric: true },
                    { key: "d", label: "Change", numeric: true },
                  ]}
                >
                  {job.result.metrics.map((m) => (
                    <tr key={m.label}>
                      <td>{m.label}</td>
                      <td className="num">{fmtMetric(m.actual, m.unit)}</td>
                      <td className="num">{fmtMetric(m.scenario, m.unit)}</td>
                      <td
                        className={`num ${
                          m.change != null ? (m.change >= 0 ? "up" : "down") : ""
                        }`}
                      >
                        {fmtChange(m.change, m.unit)}
                      </td>
                    </tr>
                  ))}
                </DataTable>
              </Card>
              <Card label="Equity" title="Equity" heading="section">
                <TwoLineChart
                  scenario={job.result.equity}
                  actual={job.result.actual}
                />
                <div className="chart-legend">
                  <span className="chart-legend__item">
                    <i className="chart-marker whatif-marker--scenario" />{" "}
                    Scenario equity
                  </span>
                  <span className="chart-legend__item">
                    <i className="chart-marker whatif-marker--actual" /> What
                    actually happened
                  </span>
                </div>
              </Card>
              {job.result.removed.length > 0 && (
                <Card
                  title={`Trades the scenario would not have taken · ${
                    job.result.removed.length
                  } of ${
                    job.result.metrics.find((m) => m.label === "Trades")
                      ?.actual ?? "—"
                  }`}
                >
                  <DataTable
                    minWidth={520}
                    columns={[
                      { key: "pair", label: "Pair" },
                      { key: "when", label: "Opened" },
                      { key: "why", label: "Why" },
                      { key: "r", label: "R", numeric: true },
                    ]}
                  >
                    {job.result.removed.map((t, i) => (
                      <tr key={i}>
                        <td>{t.pair}</td>
                        <td>{formatDateTime(t.opened_at)}</td>
                        <td>{t.reason}</td>
                        <td className="num">{t.realised_r ?? "—"}</td>
                      </tr>
                    ))}
                  </DataTable>
                </Card>
              )}
              {job.result.added.length > 0 && (
                <Card
                  title={`Trades the scenario added · ${job.result.added.length}`}
                >
                  <DataTable
                    minWidth={520}
                    columns={[
                      { key: "pair", label: "Pair" },
                      { key: "when", label: "Opened" },
                      { key: "why", label: "Why" },
                      { key: "r", label: "R", numeric: true },
                    ]}
                  >
                    {job.result.added.map((t, i) => (
                      <tr key={i}>
                        <td>{t.pair}</td>
                        <td>{formatDateTime(t.opened_at)}</td>
                        <td>{t.stand_in ? "Rules stand-in" : t.reason}</td>
                        <td className="num">{t.realised_r ?? "—"}</td>
                      </tr>
                    ))}
                  </DataTable>
                </Card>
              )}
              {job.result.stand_ins > 0 && (
                <p className="card__meta">
                  {job.result.stand_ins} trades came from the Rules stand-in:
                  the model never saw those slots.
                </p>
              )}
            </>
          )}
          {!running && !done && (
            <Card>
              <EmptyState
                title="Pick a period, change a setting, press Run"
                footer="Scenarios never touch the real account."
              />
            </Card>
          )}
        </div>
      </div>

      {applyOpen && jobId && (
        <Dialog
          title="Apply these settings?"
          onClose={() => setApplyOpen(false)}
          actions={[
            { label: "Cancel", onClick: () => setApplyOpen(false) },
            {
              label: apply.isPending ? "Applying…" : "Apply",
              variant: "primary",
              onClick: () =>
                apply.mutate(jobId, {
                  onError: (e) => setError(String(e)),
                  onSettled: () => setApplyOpen(false),
                }),
            },
          ]}
        >
          <p>
            The scenario's settings become the live settings. The change is
            logged in the settings history.
          </p>
        </Dialog>
      )}
    </AppShell>
  );
}

function fmtMetric(v: number | null, unit: string): string {
  if (v == null) return "—";
  switch (unit) {
    case "pct":
      return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
    case "eur":
      return `€${v.toFixed(2)}`;
    case "r":
      return `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
    default:
      return String(Math.round(v));
  }
}

function fmtChange(v: number | null, unit: string): string {
  if (v == null) return "—";
  const sign = v >= 0 ? "+" : "−";
  const abs = Math.abs(v);
  switch (unit) {
    case "pct":
      return `${sign}${abs.toFixed(2)} pts`;
    case "eur":
      return `${sign}${abs.toFixed(2)}`;
    case "r":
      return `${sign}${abs.toFixed(2)}`;
    default:
      return `${sign}${Math.round(abs)}`;
  }
}

function TwoLineChart({
  scenario,
  actual,
}: {
  scenario: { time: string; value: number | null }[];
  actual: { time: string; value: number | null }[];
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const style = getComputedStyle(document.documentElement);
    const css = (name: string) => style.getPropertyValue(name).trim();
    const chart: IChartApi = createChart(ref.current, {
      height: 300,
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
    });
    const toData = (pts: { time: string; value: number | null }[]) =>
      pts
        .filter((p) => p.value != null)
        .map((p) => ({
          time: Math.floor(new Date(p.time).getTime() / 1000) as never,
          value: p.value as number,
        }));
    chart
      .addSeries(LineSeries, {
        color: css("--accent"),
        lineWidth: 2,
        priceLineVisible: false,
      })
      .setData(toData(scenario));
    chart
      .addSeries(LineSeries, {
        color: css("--text-muted"),
        lineWidth: 1,
        priceLineVisible: false,
      })
      .setData(toData(actual));
    chart.timeScale().fitContent();
    const observer = new ResizeObserver(() => {
      if (ref.current) chart.applyOptions({ width: ref.current.clientWidth });
    });
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, [scenario, actual]);

  return <div ref={ref} className="chart-box" style={{ height: 300 }} />;
}
