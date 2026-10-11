import type { ReactNode } from "react";
import { useGlossaryDef } from "./glossaryContext";

/**
 * A glossary term: dotted underline with a plain-language tooltip. The
 * tooltip text is looked up in the backend glossary by `term`
 * (case-insensitive); `tip` overrides the lookup. With plain-language mode
 * off or an unknown term, the children render without the underline.
 */
export function GlossaryTerm({
  term,
  tip,
  children,
}: {
  term?: string;
  tip?: string;
  children: ReactNode;
}) {
  const text = useGlossaryDef(term ?? "") ?? tip;
  if (!text) return <>{children}</>;
  return (
    <span className="gterm" title={text}>
      {children}
    </span>
  );
}
