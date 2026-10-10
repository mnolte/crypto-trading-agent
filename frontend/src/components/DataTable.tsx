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
}: {
  columns: Column[];
  children: ReactNode;
  minWidth?: number;
}) {
  return (
    <div className="table-scroll">
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
