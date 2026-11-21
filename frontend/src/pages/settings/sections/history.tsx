import type { RowDef } from "../controls";
import { HistoryBlock } from "../blocks";

export const rows: RowDef[] = [];

export function Section() {
  return <HistoryBlock />;
}
