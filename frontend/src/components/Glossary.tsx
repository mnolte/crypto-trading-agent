import { Fragment, useMemo, type ReactNode } from "react";
import { useGlossary } from "../api/hooks";
import { GlossaryContext, useGlossaryTerms } from "./glossaryContext";

/**
 * Loads the plain-language glossary once and exposes it to every
 * GlossaryTerm. Terms render as plain text when plain-language mode is off
 * or the term is unknown.
 */
export function GlossaryProvider({ children }: { children: ReactNode }) {
  const { data } = useGlossary();
  const terms = useMemo(
    () => new Map((data?.terms ?? []).map((t) => [t.term, t.definition])),
    [data],
  );
  return (
    <GlossaryContext.Provider value={data?.plain_language ? terms : new Map()}>
      {children}
    </GlossaryContext.Provider>
  );
}

const WORD_CHAR = /[A-Za-z0-9]/;

/** Split `text` around glossary terms; longest terms win (ema 200 > ema). */
function splitTerms(
  text: string,
  terms: Map<string, string>,
): { text: string; def?: string }[] {
  if (terms.size === 0 || !text) return [{ text }];
  const sorted = [...terms.keys()].sort((a, b) => b.length - a.length);
  const lower = text.toLowerCase();
  const out: { text: string; def?: string }[] = [];
  let i = 0;
  while (i < text.length) {
    let hit: string | undefined;
    for (const term of sorted) {
      if (
        lower.startsWith(term, i) &&
        !WORD_CHAR.test(text[i - 1] ?? "") &&
        !WORD_CHAR.test(text[i + term.length] ?? "")
      ) {
        hit = term;
        break;
      }
    }
    const end = hit ? i + hit.length : i + 1;
    if (hit) {
      out.push({ text: text.slice(i, end), def: terms.get(hit) });
    } else if (out.length > 0 && out[out.length - 1].def === undefined) {
      out[out.length - 1].text += text[i];
    } else {
      out.push({ text: text[i] });
    }
    i = end;
  }
  return out;
}

/** Plain text with every glossary term underlined and tooltiped. */
export function GlossaryText({ text }: { text: string }) {
  const terms = useGlossaryTerms();
  const parts = useMemo(() => splitTerms(text, terms), [text, terms]);
  return (
    <>
      {parts.map((p, i) =>
        p.def ? (
          <span key={i} className="gterm" title={p.def}>
            {p.text}
          </span>
        ) : (
          <Fragment key={i}>{p.text}</Fragment>
        ),
      )}
    </>
  );
}
