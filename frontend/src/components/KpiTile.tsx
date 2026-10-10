import type { ReactNode } from "react";
import { GlossaryTerm } from "./GlossaryTerm";
import { Meter, type MeterColor } from "./Meter";

/** Label, big mono value, sub-line, optional bar. */
export function KpiTile({
  label,
  value,
  sub,
  subTone,
  meter,
  glossary,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  subTone?: "up" | "down";
  meter?: { pct: number; color?: MeterColor };
  glossary?: string;
}) {
  return (
    <div className="kpi">
      <div className="kpi__label">
        {glossary ? <GlossaryTerm tip={glossary}>{label}</GlossaryTerm> : label}
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
