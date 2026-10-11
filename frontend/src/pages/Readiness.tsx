import type { ReadinessRowOut } from "../api/client";
import { useReadiness } from "../api/hooks";
import { AppShell } from "../components/AppShell";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { DataTable } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { Meter } from "../components/Meter";
import { formatDateTime } from "../lib/format";

/** The live-readiness checklist: nine criteria, all computed in code. The
 * Live switch stays visibly disabled in v0.1 either way. */
export function Readiness() {
  const { data } = useReadiness();
  return (
    <AppShell
      title="Live readiness"
      actions={
        data && (
          <span className="card__meta">
            Paper account #{data.account_id} since{" "}
            {formatDateTime(data.account_started_at)} · checked{" "}
            {formatDateTime(data.checked_at)}
          </span>
        )
      }
    >
      {data ? (
        <>
          <div className="row">
            <Card className="row__main readiness-summary" label="Summary">
              <ProgressRing met={data.met} total={data.total} />
              <div className="readiness-summary__text">
                <span className="readiness-summary__status">{data.status}</span>
                <span className="readiness-summary__body">{data.summary}</span>
                {data.estimate && (
                  <span className="card__meta">{data.estimate}</span>
                )}
              </div>
            </Card>
            <section className="card card--locked row__side" aria-label="Live trading">
              <div className="locked__head">
                <svg
                  width="20"
                  height="20"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <rect x="5" y="11" width="14" height="10" rx="2" />
                  <path d="M8 11V7a4 4 0 0 1 8 0v4" />
                </svg>
                <span>Live trading is locked</span>
              </div>
              <p className="locked__body">
                Live trading is not part of v0.1. When it is built, this
                switch unlocks only when all {data.total} criteria are met,
                and you will still have to confirm it yourself.
              </p>
              <Button variant="secondary" disabled>
                Switch to live
              </Button>
            </section>
          </div>

          <Card label="Criteria">
            <DataTable
              minWidth={820}
              columns={[
                { key: "criterion", label: "Criterion" },
                { key: "now", label: "Now", numeric: true },
                { key: "needed", label: "Needed", numeric: true },
                { key: "progress", label: "Progress" },
                { key: "status", label: "Status" },
              ]}
            >
              {data.rows.map((row) => (
                <CriterionRow key={row.id} row={row} />
              ))}
            </DataTable>
            <p className="card__meta readiness-foot">
              Thresholds can be changed in Settings → Live readiness.
              Loosening a threshold is logged and shown here.
            </p>
          </Card>
        </>
      ) : (
        <EmptyState title="Loading checklist…" />
      )}
    </AppShell>
  );
}

function ProgressRing({ met, total }: { met: number; total: number }) {
  const r = 46;
  const circ = 2 * Math.PI * r;
  return (
    <svg
      width="112"
      height="112"
      viewBox="0 0 112 112"
      role="img"
      aria-label={`${met} of ${total} criteria met`}
      className="readiness-ring"
    >
      <circle cx="56" cy="56" r={r} fill="none" stroke="var(--line)" strokeWidth="10" />
      <circle
        cx="56"
        cy="56"
        r={r}
        fill="none"
        stroke="var(--accent)"
        strokeWidth="10"
        strokeLinecap="round"
        strokeDasharray={`${(circ * met) / total} ${circ}`}
        transform="rotate(-90 56 56)"
      />
      <text x="56" y="54" textAnchor="middle" className="readiness-ring__count">
        {met}/{total}
      </text>
      <text x="56" y="74" textAnchor="middle" className="readiness-ring__sub">
        criteria met
      </text>
    </svg>
  );
}

function CriterionRow({ row }: { row: ReadinessRowOut }) {
  return (
    <tr>
      <td>
        <span className="criterion__name">{row.name}</span>
        <span className="criterion__why">{row.why}</span>
        {row.loosened && <span className="criterion__loosened">{row.loosened}</span>}
      </td>
      <td className="num">{row.value_text}</td>
      <td className="num">{row.target_text}</td>
      <td className="criterion__meter">
        <Meter pct={row.progress * 100} color={row.met ? "up" : "accent"} />
      </td>
      <td>
        <span className={`pill ${row.met ? "pill--met" : "pill--unmet"}`}>
          {row.met ? "Met" : `Not yet${row.hint ? ` · ${row.hint}` : ""}`}
        </span>
      </td>
    </tr>
  );
}
