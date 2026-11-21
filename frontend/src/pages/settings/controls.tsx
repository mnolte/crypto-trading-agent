import { useState, type ReactNode } from "react";
import type {
  Settings as SettingsType,
  SettingsMetaItem,
  SettingsPreviewOut,
} from "../../api/client";
import { Input, Select } from "../../components/FormField";
import { parseNumberInput } from "../../lib/format";
import {
  defaultText,
  formatLeaf,
  getPath,
  leafChanged,
  setPath,
  type MetaMap,
} from "./model";

/** Props every section component accepts. */
export interface SectionProps {
  ctx: Ctx;
  query?: string;
  onlyChanged?: boolean;
}

/** Everything a section component needs from the page. */
export interface Ctx {
  draft: SettingsType;
  /** Set one dotted key on the draft. */
  set: (key: string, value: unknown) => void;
  /** Saved (last persisted) document, for "changed" marks. */
  saved: SettingsType;
  meta: MetaMap;
  /** Latest POST /api/settings/preview result, when present. */
  preview?: SettingsPreviewOut;
  /** prompt_version choices live outside meta; filled per request. */
  promptVersions?: string[];
}

/** One row on a section page. `keys` are the dotted settings leaves the
 * row edits (or, for custom blocks, the leaves it stands for). */
export interface RowDef {
  keys: string[];
  label: string;
  explanation: string;
  /** Small group label rendered above the row ("Decisions", …). */
  heading?: string;
  /** Shown only inside the section's Advanced fold / search. */
  advanced?: boolean;
  /** Full custom renderer; receives the row shell's inner area. */
  render?: (ctx: Ctx) => ReactNode;
  /** Custom blocks (tables, forms) span the whole row width. */
  wide?: boolean;
}

export function rowChanged(ctx: Ctx, keys: string[]): boolean {
  return keys.some((k) => leafChanged(ctx.draft, ctx.saved, k));
}

/** "Changed from 1.0% · Reset" under a changed row. */
function ChangedNote({ ctx, keys }: { ctx: Ctx; keys: string[] }) {
  const first = keys.find((k) => leafChanged(ctx.draft, ctx.saved, k));
  if (!first) return null;
  const item = ctx.meta[first];
  const oldText = formatLeaf(getPath(ctx.saved, first), item?.unit);
  return (
    <span className="set-row__changed">
      Changed from {oldText}
      <button
        type="button"
        className="set-row__reset"
        onClick={() =>
          keys.forEach((k) => {
            const m = ctx.meta[k];
            if (m) ctx.set(k, m.default);
          })
        }
      >
        Reset
      </button>
    </span>
  );
}

function ErrorNote({ ctx, keys }: { ctx: Ctx; keys: string[] }) {
  const errs = keys
    .map((k) => ctx.preview?.errors?.[k])
    .filter((e): e is string => Boolean(e));
  if (errs.length === 0) return null;
  return (
    <span className="field__msg" role="alert">
      {errs[0]}
    </span>
  );
}

function Equivalent({ ctx, keys }: { ctx: Ctx; keys: string[] }) {
  const eq = keys
    .map((k) => ctx.preview?.equivalents?.[k])
    .filter((e): e is string => Boolean(e));
  if (eq.length === 0) return null;
  return <span className="set-row__eq">= {eq.join(" · ")}</span>;
}

/** The one-row layout: name + explanation left, control right. */
export function Row({ ctx, def }: { ctx: Ctx; def: RowDef }) {
  const item = ctx.meta[def.keys[0] ?? ""];
  const changed = rowChanged(ctx, def.keys);
  return (
    <div
      className={`set-row${changed ? " set-row--changed" : ""}${def.wide ? " set-row--wide" : ""}`}
      data-setting={def.keys[0] ?? def.label}
    >
      {def.heading && <h3 className="set-sub">{def.heading}</h3>}
      <div className="set-row__text">
        <span className="set-row__label">{def.label}</span>
        <span className="set-row__expl">
          {def.explanation}
          {item && !item.fixed && (
            <span className="set-row__default"> · {defaultText(item)}</span>
          )}
          {item?.fixed && <span className="set-row__default"> · fixed by the spec</span>}
        </span>
        {changed && <ChangedNote ctx={ctx} keys={def.keys} />}
      </div>
      <div className={`set-row__control${def.wide ? " set-row__control--wide" : ""}`}>
        {def.render ? (
          def.render(ctx)
        ) : (
          <AutoControl ctx={ctx} def={def} item={item} />
        )}
        <Equivalent ctx={ctx} keys={def.keys} />
        <ErrorNote ctx={ctx} keys={def.keys} />
      </div>
    </div>
  );
}

function AutoControl({
  ctx,
  def,
  item,
}: {
  ctx: Ctx;
  def: RowDef;
  item: SettingsMetaItem | undefined;
}) {
  const key = def.keys[0];
  if (!item) return null;
  if (item.fixed) {
    return <span className="set-row__fixed">{formatLeaf(getPath(ctx.draft, key), item.unit)}</span>;
  }
  const value = getPath(ctx.draft, key);
  const choices =
    key === "model.prompt_version" && ctx.promptVersions?.length
      ? ctx.promptVersions
      : item.choices ?? undefined;
  if (choices && choices.length > 0) {
    return (
      <Select
        aria-label={def.label}
        value={String(value ?? "")}
        onChange={(e) => ctx.set(key, e.target.value)}
      >
        {choices.map((c) => (
          <option key={c} value={c}>
            {CHOICE_LABELS[c] ?? c}
          </option>
        ))}
        {value != null && !choices.includes(String(value)) && (
          <option value={String(value)}>{String(value)}</option>
        )}
      </Select>
    );
  }
  if (typeof item.default === "boolean" || typeof value === "boolean") {
    return (
      <input
        type="checkbox"
        role="switch"
        aria-checked={Boolean(value)}
        aria-label={def.label}
        className="switch"
        checked={Boolean(value)}
        onChange={(e) => ctx.set(key, e.target.checked)}
      />
    );
  }
  const numericDefault =
    item.default !== null &&
    item.default !== "" &&
    !Number.isNaN(Number(item.default));
  if (!numericDefault) {
    return (
      <Input
        aria-label={def.label}
        value={value === null || value === undefined ? "" : String(value)}
        onChange={(e) => ctx.set(key, e.target.value)}
      />
    );
  }
  return (
    <span className="set-row__num">
      <Input
        mono
        type="number"
        step={item.step ?? "any"}
        min={item.min ?? undefined}
        max={item.max ?? undefined}
        aria-label={def.label}
        value={value === null || value === undefined ? "" : String(value)}
        onChange={(e) => {
          const raw = e.target.value;
          if (raw === "") return;
          const parsed = parseNumberInput(raw);
          if (parsed !== undefined) ctx.set(key, parsed);
        }}
      />
      {item.unit && <span className="field__unit">{item.unit}</span>}
    </span>
  );
}

/** Prettier labels for enum choices (matches the design wording). */
export const CHOICE_LABELS: Record<string, string> = {
  auto: "Automatic",
  supervised: "Supervised",
  manual: "Manual",
  openai: "OpenAI",
  rules: "Rules",
  market: "Market",
  limit: "Limit (post-only)",
  mon_08: "Monday 08:00",
  sun_20: "Sunday 20:00",
  off: "Off",
  bitvavo: "Bitvavo live prices",
  sim: "Simulated",
};

/** Collapsed "Advanced · n settings" fold. */
export function Advanced({
  title,
  count,
  open,
  onToggle,
  children,
}: {
  title: string;
  count?: number;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className="set-advanced">
      <button type="button" className="set-advanced__toggle" onClick={onToggle}>
        {open ? "▾" : "▸"} {title}
        {count !== undefined && (
          <span className="set-advanced__count">
            {count} setting{count === 1 ? "" : "s"}
            {" · all at default"}
          </span>
        )}
      </button>
      {open && <div className="set-advanced__body">{children}</div>}
    </div>
  );
}

/** Does a row match the search text (label, explanation or key)? */
export function rowMatches(def: RowDef, query: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  return (
    def.label.toLowerCase().includes(q) ||
    def.explanation.toLowerCase().includes(q) ||
    def.keys.some((k) => k.toLowerCase().includes(q))
  );
}

/**
 * Renders a section's rows: plain rows first, advanced ones inside a fold.
 * `query` filters by text; `onlyChanged` keeps rows that differ from the
 * saved document; a search hit inside Advanced opens the fold.
 */
export function SectionBody({
  ctx,
  rows,
  query = "",
  onlyChanged = false,
  children,
}: {
  ctx: Ctx;
  rows: RowDef[];
  query?: string;
  onlyChanged?: boolean;
  children?: ReactNode;
}) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const visible = rows.filter((def) => {
    if (onlyChanged && !rowChanged(ctx, def.keys)) return false;
    if (!rowMatches(def, query)) return false;
    return true;
  });
  const plain = visible.filter((d) => !d.advanced);
  const adv = visible.filter((d) => d.advanced);
  const advForced = query !== "" && adv.length > 0;
  const showAdv = advancedOpen || advForced;
  let lastHeading: string | undefined;
  const renderRow = (def: RowDef) => {
    if (def.keys.length === 0 && def.label === "") {
      // Heading-only separator row.
      return def.heading ? (
        <h3 key={def.heading} className="set-sub">
          {def.heading}
        </h3>
      ) : null;
    }
    const heading = def.heading !== lastHeading ? def.heading : undefined;
    lastHeading = def.heading;
    return <Row key={def.label} ctx={ctx} def={{ ...def, heading }} />;
  };
  return (
    <div className="set-rows">
      {plain.map(renderRow)}
      {adv.length > 0 && (
        <Advanced
          title="Advanced"
          count={rows.filter((d) => d.advanced).length}
          open={showAdv}
          onToggle={() => setAdvancedOpen((o) => !o)}
        >
          {adv.map(renderRow)}
        </Advanced>
      )}
      {children}
    </div>
  );
}

/** A small numeric input for composite rows (several keys in one row). */
export function NumInput({
  ctx,
  k,
  label,
  unit,
}: {
  ctx: Ctx;
  k: string;
  label: string;
  unit?: string;
}) {
  const item = ctx.meta[k];
  const shown = unit ?? item?.unit;
  const value = getPath(ctx.draft, k);
  return (
    <span className="set-row__num">
      <Input
        mono
        type="number"
        step={item?.step ?? "any"}
        min={item?.min ?? undefined}
        max={item?.max ?? undefined}
        aria-label={label}
        value={value === null || value === undefined ? "" : String(value)}
        onChange={(e) => {
          const parsed = parseNumberInput(e.target.value);
          if (parsed !== undefined) ctx.set(k, parsed);
        }}
      />
      {shown && <span className="field__unit">{shown}</span>}
    </span>
  );
}

/** A text input for composite rows. */
export function TextInput({
  ctx,
  k,
  label,
  type = "text",
}: {
  ctx: Ctx;
  k: string,
  label: string,
  type?: string;
}) {
  const value = getPath(ctx.draft, k);
  return (
    <Input
      mono={type !== "time"}
      type={type}
      aria-label={label}
      value={value === null || value === undefined ? "" : String(value)}
      onChange={(e) => ctx.set(k, e.target.value)}
    />
  );
}

/** Boolean switch for composite rows. */
export function BoolInput({ ctx, k, label }: { ctx: Ctx; k: string; label: string }) {
  const value = getPath(ctx.draft, k);
  return (
    <input
      type="checkbox"
      role="switch"
      aria-checked={Boolean(value)}
      aria-label={label}
      className="switch"
      checked={Boolean(value)}
      onChange={(e) => ctx.set(k, e.target.checked)}
    />
  );
}

/** Imperative setter used by custom blocks that live outside the draft
 * (sources, SMTP, markets) — re-exported for clarity. */
export { setPath };
