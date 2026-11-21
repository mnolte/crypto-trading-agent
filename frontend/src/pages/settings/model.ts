import type { SettingsMetaItem, Settings as SettingsType } from "../../api/client";

/** One navigation entry: id is the /settings/:section param. */
export interface SectionDef {
  id: string;
  label: string;
}

export interface SectionGroup {
  group: string;
  sections: SectionDef[];
}

/** The five nav groups from the design, in order. */
export const SECTION_GROUPS: SectionGroup[] = [
  {
    group: "Trading",
    sections: [
      { id: "markets", label: "Markets" },
      { id: "execution", label: "Execution" },
      { id: "risk", label: "Risk limits" },
      { id: "exits", label: "Exits" },
      { id: "rules", label: "Entry rules" },
      { id: "broker", label: "Paper broker" },
    ],
  },
  {
    group: "Model",
    sections: [
      { id: "model", label: "Model and reviews" },
      { id: "shadows", label: "Shadows" },
    ],
  },
  {
    group: "Alerts and display",
    sections: [
      { id: "notify", label: "Notifications" },
      { id: "display", label: "Display" },
    ],
  },
  {
    group: "Connections",
    sections: [{ id: "keys", label: "API keys and data" }],
  },
  {
    group: "Records",
    sections: [
      { id: "ready", label: "Readiness" },
      { id: "schedule", label: "Schedule" },
      { id: "history", label: "History" },
      { id: "logs", label: "Logs" },
      { id: "paper", label: "Paper account" },
    ],
  },
];

export const SECTIONS: SectionDef[] = SECTION_GROUPS.flatMap((g) => g.sections);
export const SECTION_IDS = new Set(SECTIONS.map((s) => s.id));
export const DEFAULT_SECTION = "risk";
const LAST_SECTION_KEY = "settings.section";

export function sectionLabel(id: string): string {
  return SECTIONS.find((s) => s.id === id)?.label ?? id;
}

export function readLastSection(): string {
  try {
    const v = window.localStorage.getItem(LAST_SECTION_KEY);
    return v && SECTION_IDS.has(v) ? v : DEFAULT_SECTION;
  } catch {
    return DEFAULT_SECTION;
  }
}

export function writeLastSection(id: string): void {
  try {
    window.localStorage.setItem(LAST_SECTION_KEY, id);
  } catch {
    /* storage unavailable */
  }
}

/** Dotted-path get on the settings document. */
export function getPath(doc: unknown, path: string): unknown {
  let cur = doc;
  for (const part of path.split(".")) {
    if (cur === null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[part];
  }
  return cur;
}

/** Dotted-path set; returns a new document (structural sharing). */
export function setPath(doc: unknown, path: string, value: unknown): unknown {
  const parts = path.split(".");
  const clone = (v: unknown): unknown => {
    if (Array.isArray(v)) return [...v];
    if (v !== null && typeof v === "object") return { ...(v as object) };
    return v;
  };
  const rec = (node: unknown, i: number): unknown => {
    const obj = clone(node) as Record<string, unknown>;
    if (i === parts.length - 1) {
      obj[parts[i]] = value;
    } else {
      obj[parts[i]] = rec(obj[parts[i]], i + 1);
    }
    return obj;
  };
  return rec(doc, 0);
}

/** Leaf map of the document: arrays and scalars are leaves. */
export function flattenDoc(doc: unknown, prefix = ""): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (doc === null || typeof doc !== "object" || Array.isArray(doc)) {
    if (prefix) out[prefix] = doc;
    return out;
  }
  for (const [k, v] of Object.entries(doc as Record<string, unknown>)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      Object.assign(out, flattenDoc(v, key));
    } else {
      out[key] = v;
    }
  }
  return out;
}

/** "1.0" and 1 are the same setting; "0.10" and "0.1" too. */
export function sameValue(a: unknown, b: unknown): boolean {
  if (typeof a === "boolean" || typeof b === "boolean") return a === b;
  if (a === null || a === undefined || b === null || b === undefined) {
    return a === b || (a == null && b == null);
  }
  const na = Number(a);
  const nb = Number(b);
  if (a !== "" && b !== "" && !Number.isNaN(na) && !Number.isNaN(nb)) {
    return na === nb;
  }
  return String(a) === String(b);
}

export function leafChanged(
  draft: SettingsType | null,
  saved: SettingsType | null,
  key: string,
): boolean {
  if (!draft || !saved) return false;
  return !sameValue(getPath(draft, key), getPath(saved, key));
}

/** Keys whose draft value differs from the saved document. */
export function changedKeys(
  draft: SettingsType,
  saved: SettingsType,
): Set<string> {
  const flatD = flattenDoc(draft);
  const flatS = flattenDoc(saved);
  const keys = new Set([...Object.keys(flatD), ...Object.keys(flatS)]);
  const out = new Set<string>();
  for (const k of keys) {
    if (!sameValue(flatD[k], flatS[k])) out.add(k);
  }
  return out;
}

/** Keys whose draft value differs from the meta default. */
export function nonDefaultKeys(
  draft: SettingsType,
  meta: Record<string, SettingsMetaItem>,
): Set<string> {
  const flat = flattenDoc(draft);
  const out = new Set<string>();
  for (const [k, item] of Object.entries(meta)) {
    if (item.section === null) continue;
    if (!sameValue(flat[k], item.default)) out.add(k);
  }
  return out;
}

const UNIT_TEXT: Record<string, string> = {
  "%": "%",
  "€": "€",
  x: "×",
  s: "s",
  h: "h",
  days: "days",
  bars: "bars",
  ATR: "ATR",
  calls: "calls",
  MB: "MB",
  pts: "pts",
  positions: "positions",
  losses: "losses",
  conditions: "conditions",
  pairs: "pairs",
  a: "a day",
  "a day": "a day",
  decisions: "decisions",
  trades: "trades",
  Amsterdam: "Amsterdam",
};

/** Human label for a leaf value given its meta unit. */
export function formatLeaf(value: unknown, unit?: string | null): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "On" : "Off";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
  const u = unit ? UNIT_TEXT[unit] ?? unit : "";
  if (typeof value === "number" || (typeof value === "string" && value !== "" && !Number.isNaN(Number(value)))) {
    const n = Number(value);
    const text = Number.isInteger(n) ? String(n) : String(n);
    if (u === "€") return `€${text}`;
    if (u === "%") return `${text}%`;
    if (u === "×") return `${text}×`;
    return u ? `${text} ${u}` : text;
  }
  return String(value);
}

/** "· default X" suffix text for a row. */
export function defaultText(item: SettingsMetaItem | undefined): string {
  if (!item) return "";
  if (item.fixed) return "fixed by the spec";
  return `default ${formatLeaf(item.default, item.unit)}`;
}

export type MetaMap = Record<string, SettingsMetaItem>;
