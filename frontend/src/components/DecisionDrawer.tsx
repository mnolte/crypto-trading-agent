import { Link } from "react-router-dom";
import type { Tf } from "../api/client";
import { useDecision } from "../api/hooks";
import {
  formatChange,
  formatDateTime,
  formatEur,
  formatPrice,
  formatQty,
  formatTimeTz,
} from "../lib/format";
import { ActionBadge } from "./ActionBadge";
import { EmptyState } from "./EmptyState";
import { GateList } from "./GateList";
import { Meter } from "./Meter";
import { RegimeChip } from "./RegimeChip";
import { Drawer } from "./SlidePanel";
import { Button } from "./Button";
import { DataTable } from "./DataTable";

const TFS: Tf[] = ["1h", "4h", "1d"];

const READ_COLOR: Record<string, string> = {
  bullish: "var(--up)",
  trending: "var(--up)",
  bearish: "var(--down-text)",
  high: "var(--accent)",
  neutral: "var(--text-soft)",
  ranging: "var(--text-soft)",
  low: "var(--text-soft)",
  normal: "var(--text-soft)",
};

/**
 * The 560 px right-hand drawer that opens from "Open full reasoning" or any
 * decision marker. Outcome and LLM-inspector sections land with later steps.
 */
export function DecisionDrawer({
  id,
  onClose,
  onNavigate,
}: {
  id: string;
  onClose: () => void;
  onNavigate: (id: string) => void;
}) {
  const { data, isLoading, isError } = useDecision(id);
  const decision = data?.decision;

  const meta = decision
    ? `Cycle #${decision.cycle} · ${formatTimeTz(decision.at)} · ${
        decision.model ?? "rules baseline"
      } · prompt ${decision.prompt_version ?? "—"}`
    : undefined;

  const footer = (
    <>
      {decision && (
        <div className="slide-panel__meta">
          {data?.execution_mode} mode
          {decision.status === "executed" && decision.execute_at
            ? ` · executed ${formatTimeTz(decision.execute_at)}`
            : decision.status !== "none"
              ? ` · ${decision.status}`
              : ""}
          {data?.fill_price ? ` · filled at ${formatPrice(data.fill_price)}` : ""}
        </div>
      )}
      <div className="drawer-actions">
        <Button
          variant="secondary"
          disabled={!data?.prev_id}
          onClick={() => data?.prev_id && onNavigate(data.prev_id)}
        >
          ← Previous
        </Button>
        {decision && (
          <Link
            to={`/indicators?pair=${decision.pair}&at=${decision.id}`}
            className="btn btn--secondary drawer-actions__grow"
            onClick={onClose}
          >
            Open in Indicators
          </Link>
        )}
        <Button
          variant="secondary"
          disabled={!data?.next_id}
          onClick={() => data?.next_id && onNavigate(data.next_id)}
        >
          Next →
        </Button>
      </div>
    </>
  );

  return (
    <Drawer
      label="Decision detail"
      meta={meta}
      title={decision ? `${decision.pair} decision` : "Decision"}
      onClose={onClose}
      footer={footer}
    >
      {isLoading && <EmptyState title="Loading…" />}
      {isError && <EmptyState title="Decision not found" />}
      {decision && (
        <div className="drawer-stack">
          <div className="chip-row">
            <RegimeChip regime={decision.regime} label="regime" />
            <span className="card__meta" style={{ alignSelf: "center" }}>
              via {decision.decider} decider
            </span>
          </div>

          <section aria-label="Verdict" className="verdict">
            <ActionBadge action={decision.action} />
            <div className="verdict__body">
              <div className="num verdict__line">
                {decision.plan
                  ? `${formatQty(decision.plan.qty)} ${decision.pair.split("-")[0]} ` +
                    `@ ${formatPrice(decision.plan.entry)} ≈ ${formatEur(decision.plan.notional)}`
                  : "no order placed"}
              </div>
              <div className="verdict__conf">
                <span>confidence</span>
                <Meter pct={decision.confidence * 100} color="up" />
                <span className="num" style={{ color: "var(--text)" }}>
                  {decision.confidence.toFixed(2)}
                </span>
              </div>
            </div>
          </section>

          {decision.gates.length > 0 && (
            <section aria-label="Gates" className="drawer-section">
              <h2 className="card__title">Safety gates</h2>
              <GateList gates={decision.gates} />
            </section>
          )}

          {decision.signals.length > 0 && (
            <section aria-label="Signal stack" className="drawer-section">
              <h2 className="card__title">Signal stack</h2>
              <SignalTable decision={decision.signals} />
            </section>
          )}

          <section aria-label="Reasoning" className="drawer-section">
            <h2 className="card__title">Agent reasoning</h2>
            <div className="reasoning">{decision.reasoning}</div>
            {decision.invalidation && (
              <div className="reasoning reasoning--invalidation">
                Invalidation: {decision.invalidation}
              </div>
            )}
          </section>

          <section aria-label="Outcome" className="drawer-section">
            <h2 className="card__title">Outcome</h2>
            {data.outcomes.length > 0 || data.outcome_failures.length > 0 ? (
              <>
                <div className="kv-box">
                  {data.outcomes.map((outcome) => (
                    <div className="kv" key={outcome.horizon}>
                      <span>{outcome.horizon}h · {outcome.label}</span>
                      <span className="num">{formatChange(outcome.forward_return_pct)} · {outcome.first_touch ?? "neither"}</span>
                    </div>
                  ))}
                  {data.outcome_failures.map((failure) => (
                    <div className="kv" key={`failure-${failure.horizon}`}>
                      <span>{failure.horizon}h · unavailable</span>
                      <span>{failure.reason}</span>
                    </div>
                  ))}
                </div>
                {data.outcome_pending_until && (
                  <p className="card__meta">Next outcome pending until {formatDateTime(data.outcome_pending_until)}.</p>
                )}
              </>
            ) : (
              <p className="card__meta" style={{ margin: 0 }}>
                Pending until {data.outcome_pending_until ? formatDateTime(data.outcome_pending_until) : "the next outcome run"}.
              </p>
            )}
          </section>

          {decision.plan && (
            <section aria-label="Order plan" className="plan-grid">
              <PlanTile label="Entry" value={formatPrice(decision.plan.entry)} />
              <PlanTile
                label="Stop"
                value={formatPrice(decision.plan.stop)}
                tone="down"
              />
              <PlanTile
                label="Target"
                value={formatPrice(decision.plan.target)}
                tone="up"
              />
              <PlanTile
                label="Invalidation"
                value={
                  decision.invalidation_price
                    ? formatPrice(decision.invalidation_price)
                    : "—"
                }
                tone="warn"
                title="If price crosses this, the model is asked again at once. Not an exit."
              />
            </section>
          )}
        </div>
      )}
    </Drawer>
  );
}

function PlanTile({
  label,
  value,
  tone,
  title,
}: {
  label: string;
  value: string;
  tone?: "up" | "down" | "warn";
  title?: string;
}) {
  return (
    <div
      className={`plan-tile${tone === "warn" ? " plan-tile--warn" : ""}`}
      title={title}
    >
      <span className="plan-tile__label">{label}</span>
      <span
        className="num"
        style={{
          color:
            tone === "up"
              ? "var(--up)"
              : tone === "down"
                ? "var(--down-text)"
                : tone === "warn"
                  ? "var(--accent)"
                  : undefined,
        }}
      >
        {value}
      </span>
    </div>
  );
}

function SignalTable({
  decision,
}: {
  decision: { name: string; tf: Tf; read: string }[];
}) {
  const names = [...new Set(decision.map((s) => s.name))];
  return (
    <DataTable
      columns={[
        { key: "signal", label: "Signal" },
        ...TFS.map((tf) => ({ key: tf, label: tf, numeric: true })),
      ]}
    >
      {names.map((name) => (
        <tr key={name}>
          <td className="text">{name}</td>
          {TFS.map((tf) => {
            const cell = decision.find((s) => s.name === name && s.tf === tf);
            return (
              <td
                key={tf}
                className="num"
                style={{ color: cell ? READ_COLOR[cell.read] : undefined }}
              >
                {cell?.read ?? "—"}
              </td>
            );
          })}
        </tr>
      ))}
    </DataTable>
  );
}
