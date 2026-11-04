import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type {
  CheckOut,
  LlmCallDetail,
  LlmCallListItem,
} from "../api/client";
import {
  useCosts,
  useLlmCall,
  useLlmCalls,
  useOutcomeScorecard,
  useReplayCall,
  useSetVerdict,
  useSettings,
  useSnapshot,
} from "../api/hooks";
import { AppShell } from "../components/AppShell";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Chip } from "../components/Chip";
import { DecisionDrawer } from "../components/DecisionDrawer";
import { Dialog } from "../components/Dialog";
import { EmptyState } from "../components/EmptyState";
import { FormField, Input, Select } from "../components/FormField";
import { formatDateTime, formatEur, formatTime } from "../lib/format";

const STATUS_LABEL: Record<string, string> = {
  ok: "OK",
  invalid_output: "Invalid output",
  timeout: "Timeout",
  api_error: "API error",
  input_blocked: "Input blocked",
};

type ChipVariant = "neutral" | "up" | "down" | "amber" | "side";

function statusVariant(status: string | null): ChipVariant {
  if (status === "ok") return "up";
  if (status === "input_blocked") return "amber";
  return "down";
}

function checkVariant(status: CheckOut["status"]): ChipVariant {
  return status === "pass" ? "up" : status === "warn" ? "amber" : "down";
}

function prettyJson(text: string | null): string {
  if (!text) return "—";
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function parseJson<T>(text: string | null): T | null {
  if (!text) return null;
  try {
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

interface BriefV2 {
  previous_decision?: { outcome?: string } | null;
  trade_math?: {
    buy_feasible?: boolean;
    infeasible_reason?: string | null;
    conditions_1h_met?: number;
    conditions_1h_required?: number;
  } | null;
}

function BriefChips({ brief }: { brief: string | null }) {
  const parsed = parseJson<BriefV2>(brief);
  if (!parsed) return null;
  const tm = parsed.trade_math;
  const prev = parsed.previous_decision;
  if (!tm && !prev?.outcome) return null;
  return (
    <div className="dev-row">
      {prev?.outcome && (
        <span className="chip chip--neutral" title="previous_decision.outcome">
          previous: {prev.outcome}
        </span>
      )}
      {tm && (
        <span
          className={`chip chip--${tm.buy_feasible ? "up" : "amber"}`}
          title={tm.infeasible_reason ?? "trade_math"}
        >
          {tm.buy_feasible
            ? `buy feasible · ${tm.conditions_1h_met}/${tm.conditions_1h_required} 1h conditions`
            : `infeasible: ${tm.infeasible_reason ?? "—"}`}
        </span>
      )}
    </div>
  );
}

function actionLine(item: LlmCallListItem): string {
  const action = item.action ?? "—";
  if (item.final_action && item.final_action !== item.action) {
    return `${action} → ${item.final_action}`;
  }
  return action;
}

function costText(detail: LlmCallDetail): string {
  if (detail.cost_eur === null) return "cost unknown";
  const usd = detail.cost_usd !== null ? ` ($${detail.cost_usd})` : "";
  return `est. ${formatEur(detail.cost_eur, 4)}${usd}`;
}

function copy(text: string | null) {
  if (text) void navigator.clipboard.writeText(text);
}

export function Inspector() {
  const [params, setParams] = useSearchParams();
  const { data: snapshot } = useSnapshot();
  const { data: settings } = useSettings();
  const { data: scorecard } = useOutcomeScorecard();

  const [pair, setPair] = useState("");
  const [status, setStatus] = useState("");
  const [verdict, setVerdict] = useState("");
  const [outcome, setOutcome] = useState("");
  const [issuesOnly, setIssuesOnly] = useState(false);
  const [showSkipped, setShowSkipped] = useState(true);
  const [drawerId, setDrawerId] = useState<string | null>(null);
  const [rerunOpen, setRerunOpen] = useState(false);

  const { data: calls } = useLlmCalls({
    pair: pair || undefined,
    status: status || undefined,
    verdict: verdict || undefined,
    outcome: outcome || undefined,
    issues: issuesOnly,
    skipped: showSkipped,
  });

  const selectedId = params.get("call");
  const items = useMemo(() => calls?.items ?? [], [calls]);
  const selected =
    items.find((i) => i.id === selectedId && i.kind === "call") ?? null;
  const { data: detail } = useLlmCall(selected?.id ?? null);

  const select = (item: LlmCallListItem) => {
    if (item.kind === "skipped") {
      if (item.decision_id) setDrawerId(item.decision_id);
      return;
    }
    setParams((p) => {
      p.set("call", item.id);
      return p;
    });
  };

  return (
    <AppShell
      title="LLM inspector"
      sub="Every model call since the start, with prompt and response"
    >
      <Card title="Outcome scorecard" label="Outcome scorecard">
        {(scorecard?.items.length ?? 0) > 0 ? (
          <div className="scorecard-grid">
            {scorecard?.items.map((row) => (
              <div className="scorecard-tile" key={`${row.decider}-${row.model}-${row.prompt_version}`}>
                <strong>{row.model ?? row.decider} · {row.prompt_version ?? "rules"}</strong>
                <span className="num">{row.decisions} decisions</span>
                <span>BUY win rate <span className="num">{row.buy_win_rate_pct === null ? "—" : `${row.buy_win_rate_pct.toFixed(1)}%`}</span></span>
                <span>Average R <span className="num">{row.average_r === null ? "—" : row.average_r.toFixed(2)}</span></span>
                <span>Missed / avoided <span className="num">{row.missed} / {row.avoided}</span></span>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState title="Outcomes are still pending" footer="The scorecard fills after decisions reach their outcome horizons." />
        )}
      </Card>
      <div className="inspector">
        <Card
          title="LLM calls"
          label="LLM calls"
          className="inspector__list"
          actions={
            <span className="card__meta">
              {snapshot?.usage.calls_today ?? "—"} today
            </span>
          }
        >
          <div className="settings-fields">
            <FormField label="Pair" htmlFor="flt-pair">
              <Select
                id="flt-pair"
                value={pair}
                onChange={(e) => setPair(e.target.value)}
              >
                <option value="">All pairs</option>
                {(settings?.active_markets ?? []).map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </Select>
            </FormField>
            <FormField label="Status" htmlFor="flt-status">
              <Select
                id="flt-status"
                value={status}
                onChange={(e) => setStatus(e.target.value)}
              >
                <option value="">Any</option>
                <option value="ok">OK</option>
                <option value="invalid_output">Invalid output</option>
                <option value="timeout">Timeout</option>
                <option value="api_error">API error</option>
                <option value="input_blocked">Input blocked</option>
              </Select>
            </FormField>
            <FormField label="Verdict" htmlFor="flt-verdict">
              <Select
                id="flt-verdict"
                value={verdict}
                onChange={(e) => setVerdict(e.target.value)}
              >
                <option value="">Any</option>
                <option value="unreviewed">Unreviewed</option>
                <option value="correct">Correct</option>
                <option value="wrong">Wrong</option>
              </Select>
            </FormField>
            <FormField label="Outcome" htmlFor="flt-outcome">
              <Select id="flt-outcome" value={outcome} onChange={(e) => setOutcome(e.target.value)}>
                <option value="">Any</option>
                <option value="win">Win</option>
                <option value="loss">Loss</option>
                <option value="missed">Missed</option>
                <option value="avoided">Avoided</option>
                <option value="flat">Flat</option>
                <option value="good exit">Good exit</option>
                <option value="early exit">Early exit</option>
                <option value="neutral">Neutral</option>
              </Select>
            </FormField>
          </div>
          <div className="dev-row">
            <label className="check-label">
              <input
                type="checkbox"
                checked={issuesOnly}
                onChange={(e) => setIssuesOnly(e.target.checked)}
              />
              <span>Has issues only</span>
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={showSkipped}
                onChange={(e) => setShowSkipped(e.target.checked)}
              />
              <span>Show skipped</span>
            </label>
          </div>

          {items.length === 0 ? (
            <EmptyState title="No model calls yet">
              Calls appear here the first time the OpenAI decider runs — after
              the agent starts and an API key is set under Settings.
            </EmptyState>
          ) : (
            <ol className="call-list">
              {items.map((item) =>
                item.kind === "skipped" ? (
                  <li key={item.id}>
                    <button
                      type="button"
                      className="call-item ta-row call-item--skipped"
                      onClick={() => select(item)}
                    >
                      <span>{item.pair} · no call, Rules pre-filter held</span>
                      <span className="num">{formatTime(item.at)}</span>
                    </button>
                  </li>
                ) : (
                  <li key={item.id}>
                    <button
                      type="button"
                      className={`call-item ta-row${
                        item.id === selectedId ? " call-item--selected" : ""
                      }`}
                      aria-current={item.id === selectedId}
                      onClick={() => select(item)}
                    >
                      <span className="call-item__top">
                        <span className="call-item__pair">{item.pair}</span>
                        <span className="num call-item__time">
                          {formatTime(item.at)}
                        </span>
                      </span>
                      <span className="call-item__row">
                        <Chip variant={statusVariant(item.status ?? null)}>
                          {STATUS_LABEL[item.status ?? ""] ?? item.status}
                        </Chip>
                        {item.issues > 0 && (
                          <Chip variant="amber">
                            {item.issues}{" "}
                            {item.issues === 1 ? "warning" : "warnings"}
                          </Chip>
                        )}
                        <span className="num">{actionLine(item)}</span>
                        <span
                          className="call-item__verdict"
                          style={{
                            color:
                              item.verdict === "correct"
                                ? "var(--ta-positive)"
                                : item.verdict === "wrong"
                                  ? "var(--ta-negative-text)"
                                  : undefined,
                          }}
                        >
                          {item.verdict ?? "unreviewed"}
                        </span>
                      </span>
                    </button>
                  </li>
                ),
              )}
            </ol>
          )}
          <a
            className="btn btn--secondary"
            href="/api/llm-calls/export.jsonl"
            download
          >
            Export labelled calls (JSONL)
          </a>
        </Card>

        <CallDetail
          detail={detail ?? null}
          rerunOpen={rerunOpen}
          setRerunOpen={setRerunOpen}
          onOpenDecision={(id) => setDrawerId(id)}
        />
      </div>

      {drawerId && (
        <DecisionDrawer
          id={drawerId}
          onClose={() => setDrawerId(null)}
          onNavigate={(id) => setDrawerId(id)}
        />
      )}
    </AppShell>
  );
}

function CallDetail({
  detail,
  rerunOpen,
  setRerunOpen,
  onOpenDecision,
}: {
  detail: LlmCallDetail | null;
  rerunOpen: boolean;
  setRerunOpen: (open: boolean) => void;
  onOpenDecision: (id: string) => void;
}) {
  const setVerdict = useSetVerdict();
  const [note, setNote] = useState("");

  if (!detail) {
    return (
      <section className="inspector__detail">
        <Card>
          <EmptyState title="Select a call">
            Every call is stored exactly as sent and returned — pick one to
            see the brief, the checks and the raw response.
          </EmptyState>
        </Card>
      </section>
    );
  }

  const parsedAction =
    detail.parsed && typeof detail.parsed.action === "string"
      ? detail.parsed.action
      : null;
  const rawResponse =
    detail.response_raw ??
    (detail.parsed !== null ? JSON.stringify(detail.parsed) : null);
  const chips: string[] = [
    detail.model,
    detail.prompt_version,
    detail.latency_ms !== null ? `${detail.latency_ms.toLocaleString()} ms` : "",
    detail.input_tokens !== null || detail.output_tokens !== null
      ? `${detail.input_tokens ?? "—"} in · ${detail.output_tokens ?? "—"} out tokens`
      : "",
    costText(detail),
    detail.request_id ?? "",
  ].filter(Boolean);

  const verdictColor =
    detail.verdict === "correct"
      ? "var(--ta-positive)"
      : detail.verdict === "wrong"
        ? "var(--ta-negative-text)"
        : "var(--ta-text-2)";

  return (
    <section className="inspector__detail">
      <Card>
        <div className="call-detail__head">
          <h2 className="call-detail__title">
            {detail.pair}
            {detail.cycle !== null ? ` · cycle #${detail.cycle}` : ""}
            {detail.feature === "replay" ? " · replay" : ""}
          </h2>
          <span className="num call-item__time">
            {formatDateTime(detail.at)}
          </span>
        </div>
        <div className="dev-row">
          {chips.map((c) => (
            <span key={c} className="chip chip--neutral num">
              {c}
            </span>
          ))}
        </div>
        <div className="call-detail__actions">
          <Button variant="secondary" onClick={() => setRerunOpen(true)}>
            Re-run…
          </Button>
          <Link
            className="btn btn--secondary"
            to={`/indicators?pair=${encodeURIComponent(detail.pair)}&at=${detail.id}`}
          >
            Open in Indicators
          </Link>
          <Button
            variant="secondary"
            disabled={!detail.decision_id}
            onClick={() => detail.decision_id && onOpenDecision(detail.decision_id)}
          >
            Open decision
          </Button>
          <a
            className="btn btn--secondary"
            href={`/api/llm-calls/${detail.id}/download`}
            download
          >
            Download JSON
          </a>
          <span style={{ flex: 1 }} />
          <span className="field__label">Verdict</span>
          <div role="group" aria-label="Verdict" className="dev-row">
            <Button
              variant={detail.verdict === "correct" ? "primary" : "secondary"}
              onClick={() =>
                setVerdict.mutate({
                  id: detail.id,
                  verdict: detail.verdict === "correct" ? null : "correct",
                  note: note || detail.verdict_note || undefined,
                })
              }
            >
              Correct
            </Button>
            <Button
              variant={detail.verdict === "wrong" ? "danger" : "secondary"}
              onClick={() =>
                setVerdict.mutate({
                  id: detail.id,
                  verdict: detail.verdict === "wrong" ? null : "wrong",
                  note: note || detail.verdict_note || undefined,
                })
              }
            >
              Wrong
            </Button>
          </div>
        </div>
        {detail.verdict && (
          <div className="num" style={{ color: verdictColor, fontSize: "var(--ta-text-md)" }}>
            Marked {detail.verdict}
            {detail.verdict_at ? ` at ${formatTime(detail.verdict_at)}` : ""}
          </div>
        )}
        <FormField label="Verdict note (optional)" htmlFor="verdict-note">
          <Input
            id="verdict-note"
            placeholder="What was wrong, e.g. cites 4h RSI as bullish while it is neutral"
            defaultValue={detail.verdict_note ?? ""}
            onChange={(e) => setNote(e.target.value)}
            onBlur={(e) => {
              if (detail.verdict && e.target.value !== (detail.verdict_note ?? "")) {
                setVerdict.mutate({
                  id: detail.id,
                  verdict: detail.verdict as "correct" | "wrong",
                  note: e.target.value || undefined,
                });
              }
            }}
          />
        </FormField>
      </Card>

      <div className="inspector__panes">
        <Card
          title="Input · as sent"
          actions={
            <Button size="sm" variant="ghost" onClick={() => copy(detail.request_json)}>
              Copy
            </Button>
          }
        >
          {detail.input_checks.length > 0 && (
            <div className="dev-row">
              {detail.input_checks.map((c) => (
                <span
                  key={c.name}
                  className={`chip chip--${checkVariant(c.status)}`}
                  title={c.detail}
                >
                  {c.name}
                </span>
              ))}
            </div>
          )}
          <details className="json-details">
            <summary>System prompt · {detail.prompt_version}</summary>
            <pre className="json-block">{detail.system_prompt ?? "—"}</pre>
          </details>
          <details className="json-details">
            <summary>Model parameters</summary>
            <pre className="json-block">
              {prettyJson(
                detail.request_json
                  ? JSON.stringify(
                      (() => {
                        try {
                          const r = JSON.parse(detail.request_json ?? "{}") as Record<
                            string,
                            unknown
                          >;
                          return {
                            model: r.model,
                            temperature: r.temperature,
                            max_output_tokens: r.max_output_tokens,
                            text: r.text ? "json_schema (strict)" : null,
                          };
                        } catch {
                          return detail.request_json;
                        }
                      })(),
                    )
                  : null,
              )}
            </pre>
          </details>
          {detail.brief_upgraded && (
            <div className="dev-row">
              <span className="chip chip--amber">
                brief upgraded {detail.brief_upgraded}
              </span>
            </div>
          )}
          <BriefChips brief={detail.brief} />
          <details className="json-details" open>
            <summary>Market brief</summary>
            <pre className="json-block">{prettyJson(detail.brief)}</pre>
          </details>
          <details className="json-details">
            <summary>Response schema · {detail.prompt_version} (strict)</summary>
            <pre className="json-block">{prettyJson(detail.output_schema)}</pre>
          </details>
        </Card>

        <Card
          title="Output · as returned"
          actions={
            <Button size="sm" variant="ghost" onClick={() => copy(detail.response_raw)}>
              Copy
            </Button>
          }
        >
          {detail.output_checks.length > 0 && (
            <ul className="gate-list">
              {detail.output_checks.map((c) => (
                <li key={c.name} className="gate-row check-row">
                  <span
                    className="check-row__status"
                    style={{ color: `var(--${checkVariant(c.status) === "up" ? "up" : checkVariant(c.status) === "amber" ? "accent" : "down-text"})` }}
                  >
                    {c.status}
                  </span>
                  <span className="check-row__name">
                    {c.name}
                    {c.detail ? ` · ${c.detail}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {detail.attempts.length > 1 && (
            <div className="dev-row">
              {detail.attempts.map((a, i) => (
                <span key={i} className="chip chip--neutral num" title={a.error ?? ""}>
                  {a.outcome}
                  {a.http_status ? ` ${a.http_status}` : ""}
                  {a.latency_ms !== null ? ` · ${a.latency_ms} ms` : ""}
                </span>
              ))}
            </div>
          )}
          <details className="json-details" open>
            <summary>Raw response</summary>
            <pre className="json-block">
              {prettyJson(rawResponse)}
            </pre>
          </details>
          <div className="call-detail__final num">
            Final: {detail.decision_action ?? parsedAction ?? "—"}
            {detail.parsed &&
            (detail.parsed.stop_basis || detail.parsed.target_basis) ? (
              <span className="chip chip--neutral" style={{ marginLeft: 8 }}>
                {String(detail.parsed.stop_basis ?? "—")} →{" "}
                {String(detail.parsed.target_basis ?? "—")}
              </span>
            ) : null}
          </div>
          {detail.replays.length > 0 && (
            <div className="call-detail__replays">
              <div className="card__meta">Replays ({detail.replays.length})</div>
              {detail.replays.map((r) => (
                <div key={r.id} className="call-detail__replay">
                  <span className="num">{formatTime(r.at)}</span>
                  <span>
                    {r.model} · {r.prompt_version}
                  </span>
                  <span className="num" style={{ marginLeft: "auto" }}>
                    {r.action ?? "—"}
                    {r.differs ? " · differs" : ""}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      {rerunOpen && (
        <RerunDialog detail={detail} onClose={() => setRerunOpen(false)} />
      )}
    </section>
  );
}

function RerunDialog({
  detail,
  onClose,
}: {
  detail: LlmCallDetail;
  onClose: () => void;
}) {
  const { data: costs } = useCosts();
  const replay = useReplayCall();
  const [model, setModel] = useState(detail.model);
  const [promptVersion, setPromptVersion] = useState(detail.prompt_version);

  return (
    <Dialog
      title="Re-run this call?"
      onClose={onClose}
      actions={[
        { label: "Cancel", variant: "secondary", onClick: onClose },
        {
          label: replay.isPending ? "Re-running…" : "Re-run",
          variant: "primary",
          disabled: replay.isPending,
          onClick: () =>
            replay.mutate(
              { id: detail.id, model, promptVersion },
              { onSuccess: onClose },
            ),
        },
      ]}
      note={replay.error ? String(replay.error) : undefined}
    >
      <div className="settings-fields">
        <FormField label="Model" htmlFor="rerun-model">
          <Input
            id="rerun-model"
            mono
            value={model}
            onChange={(e) => setModel(e.target.value)}
          />
        </FormField>
        <FormField label="Prompt" htmlFor="rerun-prompt">
          <Input
            id="rerun-prompt"
            mono
            value={promptVersion}
            onChange={(e) => setPromptVersion(e.target.value)}
          />
        </FormField>
      </div>
      <p className="dialog__note">
        Spends one model call
        {detail.cost_eur !== null ? ` (est. ${formatEur(detail.cost_eur, 4)})` : ""}
        {costs
          ? `, counted toward the monthly cap: ${formatEur(costs.spent_eur)} of ${formatEur(costs.cap_eur)} used`
          : ""}
        . The replay never trades.
      </p>
    </Dialog>
  );
}
