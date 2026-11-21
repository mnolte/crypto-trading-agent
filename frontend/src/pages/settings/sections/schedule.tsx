import type { RowDef, SectionProps } from "../controls";
import { ScheduleBlock } from "../blocks";

export const rows: RowDef[] = [];

export function Section({ ctx }: SectionProps) {
  return <ScheduleBlock ctx={ctx} />;
}
