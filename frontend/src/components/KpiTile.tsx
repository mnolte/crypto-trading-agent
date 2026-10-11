import type { ReactNode } from "react";
import { GlossaryTerm } from "./GlossaryTerm";
import { Meter, type MeterColor } from "./Meter";

/** Label, big mono value, sub-line, optional bar. `glossaryTerm` names a
 * term from the backend glossary; the label gets its tooltip. */
export function KpiTile({
  label,
  value,
  sub,
  subTone,
  meter,
  glossaryTerm,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  subTone?: "up" | "down";
  meter?: { pct: number; color?: MeterColor };
  glossaryTerm?: string;
}) {
  return (
    <div className="kpi">
      <div className="kpi__label">
        {glossaryTerm ? (
          <GlossaryTerm term={glossaryTerm}>{label}</GlossaryTerm>
        ) : (
          label
        )}
      </div>
      <div className="kpi__value">{value}</div>
      {meter && <Meter pct={meter.pct} color={meter.color} />}
      {sub && (
        <div className={subTone ? `kpi__sub kpi__sub--${subTone}` : "kpi__sub"}>
          {sub}
        </div>
      )}
    </div>
  );
}
