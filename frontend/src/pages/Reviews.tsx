import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import type { BriefingOut } from "../api/client";
import { api } from "../api/client";
import { useBriefings, useLlmCall, useSettings } from "../api/hooks";
import { AppShell } from "../components/AppShell";
import { Card } from "../components/Card";
import { DataTable } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { SegmentedControl } from "../components/SegmentedControl";
import { formatDateTime, formatEur, formatSigned } from "../lib/format";

/** Weekly reviews and daily briefings: the model writes prose over facts
 * computed in code, and every number in the text was checked against them. */

interface WeeklyFacts {
  for_date?: string;
  week_start?: string;
  return_week_pct?: number | null;
  return_prev_week_pct?: number | null;
  btc_week_pct?: number | null;
  trades_closed_count?: number;
  trades_closed_prev_count?: number;
  won_count?: number;
  lost_count?: number;
  avg_r_week?: number | null;
  avg_r_prev?: number | null;
  drawdown_week_pct?: number | null;
  drawdown_prev_pct?: number | null;
  model_cost_eur?: string;
  model_calls?: number;
  trades_best?: {
    pair: string;
    realised_r?: string | null;
    realised_eur?: string | null;
    decision_href?: string | null;
  }[];
  trades_worst?: {
    pair: string;
    realised_r?: string | null;
    realised_eur?: string | null;
    decision_href?: string | null;
  }[];
  wrong_patterns?: { check: string; count: number }[];
  wrong_decisions_count?: number;
  insights_changed?: {
    dimension: string;
    key: string;
    avg_r?: number | null;
    win_rate_pct?: number | null;
  }[];
  suggestion?: {
    text: string;
    whatif_hint?: string;
    whatif_href?: string;
  } | null;
}

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function isoWeek(d: Date): number {
  const t = new Date(
    Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()),
  );
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return Math.ceil(((t.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
}

function shortDate(d: Date): string {
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

/** "Week 40 · 28 Sep – 4 Oct" from the stored week_start. */
function weekTitle(facts: WeeklyFacts, fallback: string): string {
  if (!facts.week_start) return fallback;
  const start = new Date(facts.week_start);
  const end = new Date(start.getTime() + 6 * 86400000);
  return `Week ${isoWeek(end)} · ${shortDate(start)} – ${shortDate(end)}`;
}

export function Reviews() {
  const [tab, setTab] = useState<"weekly" | "daily">("weekly");
  const [openId, setOpenId] = useState<number | null>(null);
  const { data: list } = useBriefings(tab);
  const { data: settings } = useSettings();
  const { data: detail } = useQuery({
    queryKey: ["briefing", openId],
    queryFn: () => api<BriefingOut>(`/api/briefings/${openId}`),
    enabled: openId !== null,
  });

  const items = useMemo(() => list?.items ?? [], [list]);
  const selected = openId ?? items[0]?.id ?? null;
  const weeklyLabel =
    settings?.learning.weekly_review === "mon_08"
      ? "Monday 08:00"
      : settings?.learning.weekly_review === "sun_20"
        ? "Sunday 20:00"
        : "off";

  return (
    <AppShell
      title="Reviews"
      actions={
        <span className="card__meta">
          Weekly review every {weeklyLabel} · morning briefing daily{" "}
          {settings?.learning.briefing_time ?? "08:00"}
        </span>
      }
    >
      <div className="reviews-layout">
        <Card label="Reviews" className="reviews-list">
          <SegmentedControl
            label="Review type"
            options={[
              { value: "weekly", label: "Weekly" },
              { value: "daily", label: "Briefings" },
            ]}
            value={tab}
            onChange={(v) => {
              setTab(v as "weekly" | "daily");
              setOpenId(null);
            }}
          />
          {items.length === 0 && (
            <EmptyState
              title={
                tab === "weekly" ? "No weekly review yet" : "No briefing yet"
              }
              footer={
                tab === "weekly"
                  ? "The first one writes itself on the scheduled morning."
                  : "The first briefing writes itself in the morning."
              }
            />
          )}
          {items.map((it) => {
            const facts = it.facts as WeeklyFacts;
            return (
              <button
                key={it.id}
                type="button"
                className={
                  it.id === selected
                    ? "reviews-item reviews-item--on"
                    : "reviews-item"
                }
                onClick={() => setOpenId(it.id)}
              >
                <span className="reviews-item__row">
                  <span className="reviews-item__title">
                    {it.type === "weekly"
                      ? weekTitle(facts, `Week of ${it.at.slice(0, 10)}`).split(" · ")[0]
                      : "Briefing"}
                  </span>
                  {it.type === "weekly" && facts.return_week_pct != null && (
                    <span
                      className={`num ${
                        facts.return_week_pct >= 0 ? "up" : "down"
                      }`}
                    >
                      {formatSigned(facts.return_week_pct)}%
                    </span>
                  )}
                </span>
                <span className="card__meta">
                  {it.type === "weekly" && facts.week_start
                    ? `${shortDate(new Date(facts.week_start))} – ${shortDate(
                        new Date(
                          new Date(facts.week_start).getTime() + 6 * 86400000,
                        ),
                      )}`
                    : String(facts.for_date ?? it.at.slice(0, 10))}
                </span>
              </button>
            );
          })}
        </Card>
        <div className="reviews-detail">
          {detail ? (
            <ReviewDetail briefing={detail} />
          ) : selected === null && items.length === 0 ? (
            <Card>
              <EmptyState title="No reviews yet" />
            </Card>
          ) : (
            <Card>
              <EmptyState title="Loading…" />
            </Card>
          )}
        </div>
      </div>
    </AppShell>
  );
}

function ReviewDetail({ briefing }: { briefing: BriefingOut }) {
  const facts = briefing.facts as WeeklyFacts;
  const { data: call } = useLlmCall(briefing.llm_call_id);
  const title =
    briefing.type === "weekly"
      ? weekTitle(facts, `Weekly review · ${briefing.at.slice(0, 10)}`)
      : `Briefing · ${facts.for_date ?? briefing.at.slice(0, 10)}`;
  return (
    <>
      <Card
        title={title}
        heading="section"
        actions={
          <span className="card__meta">
            Written {formatDateTime(briefing.at)}
            {call?.model ? ` · ${call.model}` : ""}
            {briefing.source === "model" ? " · numbers checked ✓" : ""}
          </span>
        }
      >
        {briefing.reason && (
          <p className="card__meta">Plain template: {briefing.reason}.</p>
        )}
        <p className="review-text">{briefing.text}</p>
        {briefing.type === "weekly" && (
          <div className="kpis reviews-kpis">
            <Kpi
              label="Return"
              value={pct(facts.return_week_pct)}
              sub={
                facts.return_prev_week_pct != null
                  ? `prior week ${pct(facts.return_prev_week_pct)}`
                  : undefined
              }
            />
            <Kpi
              label="vs holding BTC"
              value={
                facts.return_week_pct != null && facts.btc_week_pct != null
                  ? `${formatSigned(facts.return_week_pct - facts.btc_week_pct)} pts`
                  : "—"
              }
              sub={
                facts.btc_week_pct != null
                  ? `BTC ${pct(facts.btc_week_pct)}`
                  : undefined
              }
            />
            <Kpi
              label="Trades"
              value={String(facts.trades_closed_count ?? 0)}
              sub={`${facts.won_count ?? 0} won · ${facts.lost_count ?? 0} lost`}
            />
            <Kpi
              label="Average R"
              value={
                facts.avg_r_week != null
                  ? formatSigned(facts.avg_r_week)
                  : "—"
              }
              sub={
                facts.avg_r_prev != null
                  ? `prior week ${formatSigned(facts.avg_r_prev)}`
                  : undefined
              }
            />
            <Kpi
              label="Max drawdown"
              value={
                facts.drawdown_week_pct != null
                  ? `${facts.drawdown_week_pct.toFixed(1)}%`
                  : "—"
              }
              sub={
                facts.drawdown_prev_pct != null
                  ? `prior week ${facts.drawdown_prev_pct.toFixed(1)}%`
                  : undefined
              }
            />
            {facts.model_cost_eur != null && (
              <Kpi
                label="Model cost"
                value={formatEur(facts.model_cost_eur)}
                sub={
                  facts.model_calls != null
                    ? `${facts.model_calls} calls`
                    : undefined
                }
              />
            )}
          </div>
        )}
      </Card>

      {briefing.type === "weekly" && (
        <>
          <div className="row">
            <Card title="Best trades" className="row__half">
              <TradeList trades={facts.trades_best} />
            </Card>
            <Card title="Worst trades" className="row__half">
              <TradeList trades={facts.trades_worst} />
            </Card>
          </div>
          {((facts.wrong_decisions_count ?? 0) > 0 ||
            (facts.insights_changed ?? []).length > 0) && (
            <Card>
              {(facts.wrong_decisions_count ?? 0) > 0 && (
                <>
                  <h3 className="card__title">Where the model was wrong</h3>
                  <ul className="plain-list">
                    <li>
                      You marked {facts.wrong_decisions_count} decisions Wrong
                      this week.
                    </li>
                    {(facts.wrong_patterns ?? []).map((w) => (
                      <li key={w.check}>
                        {w.check} ×{w.count}
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {(facts.insights_changed ?? []).length > 0 && (
                <>
                  <h3 className="card__title">What changed in Insights</h3>
                  <ul className="plain-list">
                    {facts.insights_changed!.map((f, i) => (
                      <li key={i}>
                        {f.dimension}: {f.key}
                        {f.avg_r != null
                          ? ` · ${formatSigned(f.avg_r)}R avg`
                          : ""}
                        {f.win_rate_pct != null
                          ? ` · ${f.win_rate_pct.toFixed(0)}% win`
                          : ""}
                      </li>
                    ))}
                    <li>
                      <Link to="/insights">Open Insights</Link>
                    </li>
                  </ul>
                </>
              )}
            </Card>
          )}
          {facts.suggestion && (
            <Card title="Suggestion · not applied" variant="accent-dim">
              <div className="suggestion">
                <p className="review-text">{facts.suggestion.text}</p>
                <Link
                  to={facts.suggestion.whatif_href ?? "/what-if"}
                  className="btn btn--primary"
                >
                  {facts.suggestion.whatif_hint ?? "Try in What-if"}
                </Link>
              </div>
            </Card>
          )}
        </>
      )}
    </>
  );
}

function Kpi({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="kpi">
      <div className="kpi__label">{label}</div>
      <div className="kpi__value">{value}</div>
      {sub && <div className="kpi__sub">{sub}</div>}
    </div>
  );
}

function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function TradeList({
  trades,
}: {
  trades:
    | { pair: string; realised_r?: string | null; decision_href?: string | null }[]
    | undefined;
}) {
  if (!trades || trades.length === 0)
    return <span className="card__meta">None this week.</span>;
  return (
    <DataTable
      columns={[
        { key: "pair", label: "Pair" },
        { key: "r", label: "R", numeric: true },
        { key: "link", label: "" },
      ]}
    >
      {trades.map((t, i) => (
        <tr key={i}>
          <td>{t.pair}</td>
          <td
            className={`num ${
              t.realised_r != null
                ? Number(t.realised_r) >= 0
                  ? "up"
                  : "down"
                : ""
            }`}
          >
            {t.realised_r != null
              ? `${formatSigned(Number(t.realised_r))}R`
              : "—"}
          </td>
          <td>
            {t.decision_href && <a href={t.decision_href}>decision</a>}
          </td>
        </tr>
      ))}
    </DataTable>
  );
}
