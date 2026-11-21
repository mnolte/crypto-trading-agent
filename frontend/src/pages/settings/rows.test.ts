import { describe, expect, it } from "vitest";
import { META_SECTION_KEYS } from "./metaKeys";
import { SECTIONS } from "./model";
import { SECTION_MODULES } from "./sections";
import type { RowDef } from "./controls";

function allRows(): { section: string; def: RowDef }[] {
  return Object.entries(SECTION_MODULES).flatMap(([section, mod]) =>
    mod.rows.map((def) => ({ section, def })),
  );
}

describe("settings section rows", () => {
  it("every section in the nav has a module and vice versa", () => {
    const navIds = new Set(SECTIONS.map((s) => s.id));
    for (const id of Object.keys(SECTION_MODULES)) {
      expect(navIds.has(id), `module ${id} missing from nav`).toBe(true);
    }
    for (const id of navIds) {
      expect(SECTION_MODULES[id], `nav section ${id} has no module`).toBeDefined();
    }
  });

  it("every metadata key with a section has exactly one row", () => {
    const seen = new Map<string, string[]>();
    for (const { section, def } of allRows()) {
      for (const key of def.keys) {
        const list = seen.get(key) ?? [];
        list.push(`${section}:${def.label}`);
        seen.set(key, list);
      }
    }
    const missing = [...META_SECTION_KEYS].filter((k) => !seen.has(k));
    expect(missing, "meta keys without a row").toEqual([]);
    const duplicated = [...seen.entries()].filter(
      ([, where]) => where.length > 1,
    );
    expect(duplicated, "keys covered by more than one row").toEqual([]);
    const unknown = [...seen.keys()].filter((k) => !META_SECTION_KEYS.has(k));
    expect(unknown, "row keys that are not sectioned meta keys").toEqual([]);
  });

  it("no row claims the off-screen keys", () => {
    const offScreen = [
      "schema_version",
      "logging.queue_size",
      "logging.backtest_debug",
      "logging.library_debug",
      "storage.shutdown_grace_seconds",
    ];
    for (const { def } of allRows()) {
      for (const key of def.keys) {
        expect(offScreen, `row uses off-screen key ${key}`).not.toContain(key);
      }
    }
  });
});
