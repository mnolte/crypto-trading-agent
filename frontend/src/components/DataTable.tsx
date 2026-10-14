import type { ReactNode } from "react";

export type Column = {
  key: string;
  label: ReactNode;
  /** numbers right-align and render in IBM Plex Mono */
  numeric?: boolean;
};

/** Table wrapper: mono right-aligned numbers, horizontal scroll when narrow. */
export function DataTable({
  columns,
  children,
  minWidth,
  maxHeight,
}: {
  columns: Column[];
  children: ReactNode;
  minWidth?: number;
  /** Cap the height in px: body scrolls vertically and the header sticks. */
  maxHeight?: number;
}) {
  return (
    <div
      className={maxHeight ? "table-scroll table-scroll--cap" : "table-scroll"}
      style={maxHeight ? { maxHeight } : undefined}
    >
      <table className="dtable" style={minWidth ? { minWidth } : undefined}>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} className={c.numeric ? "num" : undefined}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
