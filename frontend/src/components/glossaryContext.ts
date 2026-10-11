import { createContext, useContext } from "react";

/** term → plain-language definition, loaded by GlossaryProvider. */
export const GlossaryContext = createContext<Map<string, string>>(new Map());

/** The definition for a term, or undefined when unknown or when
 * plain-language mode is off. */
export function useGlossaryDef(term: string): string | undefined {
  return useContext(GlossaryContext).get(term.toLowerCase());
}

/** Every loaded term, for scanning computed text. */
export function useGlossaryTerms(): Map<string, string> {
  return useContext(GlossaryContext);
}
