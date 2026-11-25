import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import type {
  Settings as SettingsType,
  SettingsPreviewOut,
} from "../api/client";
import { ApiError } from "../api/client";
import {
  usePrompts,
  useSaveSettings,
  useSettings,
  useSettingsMeta,
  useSettingsPreview,
} from "../api/hooks";
import { AppShell } from "../components/AppShell";
import { Button } from "../components/Button";
import { Dialog } from "../components/Dialog";
import { Input } from "../components/FormField";
import { Chip } from "../components/Chip";
import {
  changedKeys,
  DEFAULT_SECTION,
  flattenDoc,
  formatLeaf,
  nonDefaultKeys,
  readLastSection,
  SECTION_GROUPS,
  SECTION_IDS,
  SECTIONS,
  sectionLabel,
  setPath,
  writeLastSection,
} from "./settings/model";
import type { Ctx, RowDef } from "./settings/controls";
import { SECTION_MODULES } from "./settings/sections";

/** GET returns decimal strings; the draft is a deep clone. */
function toDraft(data: SettingsType): SettingsType {
  return JSON.parse(JSON.stringify(data)) as SettingsType;
}

export function Settings() {
  const { section: sectionParam } = useParams();
  const navigate = useNavigate();
  const { data } = useSettings();
  const { data: meta } = useSettingsMeta();
  const { data: prompts } = usePrompts();
  const save = useSaveSettings();

  const [draft, setDraft] = useState<SettingsType | null>(null);
  const [original, setOriginal] = useState<SettingsType | null>(null);
  const [version, setVersion] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  const [onlyChanged, setOnlyChanged] = useState(false);
  const [reason, setReason] = useState("");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [preview, setPreview] = useState<SettingsPreviewOut | undefined>(
    undefined,
  );
  const [confirmChanges, setConfirmChanges] = useState<
    { key: string; old?: unknown; new?: unknown; direction?: string }[] | null
  >(null);
  const [reviewOpen, setReviewOpen] = useState(false);

  const section =
    sectionParam && SECTION_IDS.has(sectionParam)
      ? sectionParam
      : (sectionParam === undefined ? readLastSection() : DEFAULT_SECTION);

  const changed = useMemo(
    () => (draft && original ? changedKeys(draft, original) : new Set<string>()),
    [draft, original],
  );
  const dirty = changed.size > 0;

  // Resync when the server document changes and the draft is clean, so an
  // external save (What-if apply, a source toggle) is not overwritten later.
  const dirtyRef = useRef(false);
  const lastData = useRef<SettingsType | undefined>(undefined);
  useEffect(() => {
    dirtyRef.current = dirty;
  });
  useEffect(() => {
    if (
      data &&
      data !== lastData.current &&
      (original === null || !dirtyRef.current)
    ) {
      lastData.current = data;
      const d = toDraft(data);
      setDraft(d);
      setOriginal(d);
    }
  }, [data, original]);

  useEffect(() => {
    writeLastSection(section);
    if (sectionParam !== section) {
      navigate(`/settings/${section}`, { replace: true });
    }
  }, [section, sectionParam, navigate]);

  // Debounced preview: 300 ms after typing stops.
  const { mutate: runPreview } = useSettingsPreview();
  const draftJson = useMemo(() => (draft ? JSON.stringify(draft) : ""), [draft]);
  useEffect(() => {
    if (!draft) return;
    const t = window.setTimeout(() => {
      runPreview(draft, {
        onSuccess: setPreview,
        onError: () => setPreview(undefined),
      });
    }, 300);
    return () => window.clearTimeout(t);
  }, [draft, draftJson, runPreview]);

  const metaMap = useMemo(() => meta?.items ?? {}, [meta]);
  const nonDefault = useMemo(
    () => (draft ? nonDefaultKeys(draft, metaMap) : new Set<string>()),
    [draft, metaMap],
  );

  // Per-section changed counts for the nav badges.
  const sectionChangedCount = useMemo(() => {
    const counts = new Map<string, number>();
    for (const k of changed) {
      const s = metaMap[k]?.section;
      if (s) counts.set(s, (counts.get(s) ?? 0) + 1);
    }
    return counts;
  }, [changed, metaMap]);
  const sectionNonDefaultCount = useMemo(() => {
    const counts = new Map<string, number>();
    for (const k of nonDefault) {
      const s = metaMap[k]?.section;
      if (s) counts.set(s, (counts.get(s) ?? 0) + 1);
    }
    return counts;
  }, [nonDefault, metaMap]);

  const previewErrors = preview?.errors ?? {};
  const hasPreviewErrors = Object.keys(previewErrors).length > 0;
  const reasonOk = reason.trim().length >= 3 && reason.trim().length <= 200;
  const canSave = dirty && !hasPreviewErrors && reasonOk && !save.isPending;

  const ctx: Ctx | null = useMemo(() => {
    if (!draft || !original) return null;
    return {
      draft,
      saved: original,
      meta: metaMap,
      preview,
      promptVersions: prompts?.map((p) => p.id),
      set: (key, value) =>
        setDraft((d) => (d ? (setPath(d, key, value) as SettingsType) : d)),
    };
  }, [draft, original, metaMap, preview, prompts]);

  // Leaving Settings with a draft asks first: beforeunload for tab close,
  // a capture-phase click guard for in-app navigation.
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
        return;
      const anchor = (e.target as HTMLElement).closest?.("a[href]");
      if (!anchor) return;
      const href = anchor.getAttribute("href") ?? "";
      if (!href.startsWith("/") || href.startsWith("/settings")) return;
      if (!window.confirm("Discard the unsaved settings changes?")) {
        e.preventDefault();
        e.stopPropagation();
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    document.addEventListener("click", onClick, true);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
      document.removeEventListener("click", onClick, true);
    };
  }, [dirty]);

  const doSave = (confirmLooser: boolean) => {
    if (!draft) return;
    save.mutate(
      { settings: draft, reason: reason.trim(), confirm_looser: confirmLooser },
      {
        onSuccess: (out) => {
          const fresh = toDraft(out.settings);
          setDraft(fresh);
          setOriginal(fresh);
          setVersion(out.version);
          setSaveError(null);
          setConfirmChanges(null);
          setReviewOpen(false);
        },
        onError: (e) => {
          if (e instanceof ApiError && e.status === 409 && e.changes) {
            setConfirmChanges(e.changes);
            return;
          }
          setSaveError(e.message);
        },
      },
    );
  };

  const discard = () => {
    if (original) {
      setDraft(toDraft(original));
      setSaveError(null);
      setReason("");
    }
  };

  const idx = SECTIONS.findIndex((s) => s.id === section);
  const prev = idx > 0 ? SECTIONS[idx - 1] : null;
  const next = idx >= 0 && idx < SECTIONS.length - 1 ? SECTIONS[idx + 1] : null;
  const mod = SECTION_MODULES[section];
  const SectionView = mod?.Section;

  // Change lines for the save bar and the confirm dialog.
  const changeLines = useMemo(() => {
    if (!draft || !original) return [] as { def: RowDef | undefined; key: string; old: unknown; new: unknown; dir?: string }[];
    const flatO = flattenDoc(draft);
    const flatS = flattenDoc(original);
    const dirs = new Map(
      (preview?.changes ?? []).map((c) => [c.key, c.direction]),
    );
    return [...changed].map((key) => ({
      key,
      old: flatS[key],
      new: flatO[key],
      dir: dirs.get(key),
      def: findRow(key),
    }));
  }, [draft, original, changed, preview]);

  return (
    <AppShell
      title="Settings"
      sub="Saved settings become a new version and apply from the next cycle."
      controls={false}
    >
      <div className="settings-layout">
        <aside className="set-nav" aria-label="Settings sections">
          <div className="set-nav__search">
            <Input
              type="search"
              placeholder="Find a setting"
              aria-label="Find a setting"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <label className="check-label set-nav__filter">
              <input
                type="checkbox"
                checked={onlyChanged}
                onChange={(e) => setOnlyChanged(e.target.checked)}
              />
              <span>
                Changed from default{" "}
                <span className="num">{nonDefault.size}</span>
              </span>
            </label>
          </div>
          <nav className="set-nav__list">
            {SECTION_GROUPS.map((g) => (
              <div key={g.group} className="set-nav__group">
                <div className="set-nav__group-label">{g.group}</div>
                {g.sections.map((s) => {
                  const n = sectionChangedCount.get(s.id) ?? 0;
                  const nd = sectionNonDefaultCount.get(s.id) ?? 0;
                  return (
                    <Link
                      key={s.id}
                      to={`/settings/${s.id}`}
                      className={`set-nav__item${s.id === section ? " set-nav__item--active" : ""}`}
                      aria-current={s.id === section ? "page" : undefined}
                    >
                      <span>{s.label}</span>
                      {nd > 0 && (
                        <span className="set-nav__count num">{nd}</span>
                      )}
                      {n > 0 && s.id !== section && (
                        <span className="set-nav__unsaved num">{n} unsaved</span>
                      )}
                    </Link>
                  );
                })}
              </div>
            ))}
          </nav>
          <div className="set-nav__mobile">
            <label className="field">
              <span className="field__label">Section</span>
              <select
                className="input"
                value={section}
                onChange={(e) => navigate(`/settings/${e.target.value}`)}
                aria-label="Section"
              >
                {SECTION_GROUPS.map((g) => (
                  <optgroup key={g.group} label={g.group}>
                    {g.sections.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.label}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>
          </div>
        </aside>

        <div className="set-main">
          {saveError && (
            <p className="settings-error" role="alert">
              {saveError}
            </p>
          )}
          <section
            className="sa-sec set-section"
            aria-labelledby={`${section}-h`}
          >
            <header className="set-section__head">
              <div>
                <h2 id={`${section}-h`}>{sectionLabel(section)}</h2>
                <SectionIntro id={section} />
              </div>
              <div className="set-section__nav">
                {prev ? (
                  <Link className="btn btn--ghost btn--sm" to={`/settings/${prev.id}`}>
                    ← {prev.label}
                  </Link>
                ) : (
                  <span />
                )}
                {next ? (
                  <Link className="btn btn--ghost btn--sm" to={`/settings/${next.id}`}>
                    {next.label} →
                  </Link>
                ) : null}
              </div>
            </header>
            {ctx && SectionView && (
              <SectionView ctx={ctx} query={query} onlyChanged={onlyChanged} />
            )}
          </section>
        </div>
      </div>

      {dirty && (
        <div className="set-savebar" role="region" aria-label="Unsaved changes">
          <button
            type="button"
            className="set-savebar__summary"
            onClick={() => setReviewOpen((o) => !o)}
            aria-expanded={reviewOpen}
          >
            <strong>{changed.size} unsaved change{changed.size === 1 ? "" : "s"}</strong>
            <span className="set-savebar__peek">
              {changeLines
                .slice(0, 2)
                .map((c) => c.def?.label ?? c.key)
                .join(" · ")}
              {changeLines.length > 2 ? " …" : ""}
            </span>
          </button>
          {reviewOpen && (
            <div className="set-savebar__review">
              <ul className="set-confirm-list">
                {changeLines.map((c) => (
                  <li key={c.key}>
                    <span>{c.def?.label ?? c.key}</span>{" "}
                    <span className="num">
                      {formatLeaf(c.old, ctx?.meta[c.key]?.unit)} →{" "}
                      {formatLeaf(c.new, ctx?.meta[c.key]?.unit)}
                    </span>{" "}
                    {c.dir && (
                      <Chip variant={c.dir === "looser" ? "amber" : "neutral"}>
                        {c.dir}
                      </Chip>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <label className="set-savebar__reason">
            <span className="field__label">Reason (shown in History)</span>
            <Input
              value={reason}
              maxLength={200}
              placeholder="Why this change?"
              aria-label="Reason (shown in History)"
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          <div className="set-savebar__actions">
            <Button variant="secondary" onClick={discard} disabled={save.isPending}>
              Discard
            </Button>
            <Button
              variant="primary"
              disabled={!canSave}
              onClick={() => doSave(false)}
            >
              {save.isPending
                ? "Saving…"
                : `Save as v${(version ?? meta?.version ?? 0) + 1}`}
            </Button>
          </div>
        </div>
      )}

      {confirmChanges && (
        <Dialog
          title="This loosens a limit"
          onClose={() => setConfirmChanges(null)}
          actions={[
            { label: "Cancel", onClick: () => setConfirmChanges(null) },
            {
              label: "Save anyway",
              variant: "primary",
              onClick: () => doSave(true),
            },
          ]}
        >
          <p>These changes loosen a limit — confirm to save them.</p>
          <ul className="set-confirm-list">
            {confirmChanges.map((c) => {
              const label = findRow(c.key)?.label ?? c.key;
              return (
                <li key={c.key}>
                  {label}:{" "}
                  <span className="num">
                    {formatLeaf(c.old, metaMap[c.key]?.unit)} →{" "}
                    {formatLeaf(c.new, metaMap[c.key]?.unit)}
                  </span>{" "}
                  <Chip variant={c.direction === "looser" ? "amber" : "neutral"}>
                    {c.direction ?? "neutral"}
                  </Chip>
                </li>
              );
            })}
          </ul>
        </Dialog>
      )}
    </AppShell>
  );
}

// Row lookup for change labels: built lazily from the section modules.
let rowIndex: Map<string, RowDef> | null = null;
function findRow(key: string): RowDef | undefined {
  if (!rowIndex) {
    rowIndex = new Map();
    for (const mod of Object.values(SECTION_MODULES)) {
      for (const def of mod.rows) {
        for (const k of def.keys) if (!rowIndex.has(k)) rowIndex.set(k, def);
      }
    }
  }
  return rowIndex.get(key);
}

const SECTION_INTROS: Record<string, string> = {
  markets:
    "Which Bitvavo EUR markets may be traded. The daily check at 01:00 UTC applies these thresholds.",
  execution: "How a decision that passes every check becomes a paper order.",
  risk: "Checked in code before every order; the model can't override them. The first one that fails blocks the action.",
  exits:
    "Checked by the exit watcher every 30 seconds, also while paused, and never waiting for a cycle or the model.",
  rules:
    "The Rules engine: the pre-filter in front of the model, and the Rules decider when the model is off. Every decision stores the version it used.",
  broker: "How paper fills are simulated against Bitvavo prices.",
  model: "The decision model and everything else that calls it.",
  shadows:
    "A shadow runs the same cycles on its own paper account with another model, prompt or entry order type. It never places orders. At most 2.",
  notify: "Which events alert you, and where. Email settings are saved with Test and save, apart from the unsaved-changes bar.",
  display: "How the app explains itself.",
  keys: "Keys are stored in the OS keyring and saved straight away, one at a time, apart from the unsaved-changes bar. Only the last 4 characters are shown.",
  ready: "Thresholds for the live-readiness checklist. The other criteria are fixed by the spec.",
  schedule: "When things run, in Amsterdam time. Read only; the times come from the settings named.",
  history: "Every saved change is a version with its reason. Promote and What-if apply save one too.",
  logs: "The log store in data/logs/logs.db, searched on the Logs screen. It is not part of the backup.",
  paper: "The simulated account and the data the agent keeps.",
};

function SectionIntro({ id }: { id: string }) {
  return <p className="set-section__intro">{SECTION_INTROS[id] ?? ""}</p>;
}
