import type { RowDef, SectionProps } from "../controls";
import { ShadowsBlock } from "../blocks";

export const rows: RowDef[] = [];

export function Section({ ctx }: SectionProps) {
  return <ShadowsBlock ctx={ctx} />;
}
