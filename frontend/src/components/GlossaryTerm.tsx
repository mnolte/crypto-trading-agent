import type { ReactNode } from "react";

/** A glossary term: dotted underline with a plain-language tooltip. */
export function GlossaryTerm({
  tip,
  children,
}: {
  tip: string;
  children: ReactNode;
}) {
  return (
    <span className="gterm" title={tip}>
      {children}
    </span>
  );
}
