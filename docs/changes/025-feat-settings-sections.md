---
item: 025
type: feat
title: Settings sections
status: merged
issue: 44
kickoff: 025-feat-settings-sections.md
spec_prs: [45]
build_prs: [51]
---

# 025 · feat · Settings sections

## 2026-11-21 · spec PR

Marc, 21 Nov: the crypto agent's settings should work like Stock Trading Agent's. New `docs/SPEC.md` → v0.3 → Settings screen; what each setting does is unchanged.

- **One section at a time** at `/settings/<section>`, in five groups (Trading, Model, Alerts and display, Connections, Records), with a Section select on a phone. Every setting the API accepts has exactly one control; five internal ones are listed as off the screen.
- **A row** per setting with its explanation and default, the euro amount next to every % of equity, fixed values as text with a lock.
- **Find a setting and Changed from default** across sections.
- **The save bar:** old → new, tighter or looser tags, a required reason (3–200 characters), "Save as v<n>"; values checked against their ranges as typed; leaving with unsaved changes asks.
- **Tighter and looser** from one map in the backend that fails closed; a save asks once more for a looser change, a move towards Automatic, a channel taken off an event, or a live model or prompt change (from PR 20: "Confirm it is you").
- **Money from the backend:** `POST /api/settings/preview` returns the euro amounts, the "What these limits mean today" sentences, the tags and the range errors, because the frontend does no money arithmetic (hard rule 2).
- **Settings versions** with reason and source; History lists them, older per-setting changes after them. Email Test and save, source on/off and market activation save their own version with a written reason; keys and the SMTP password never enter a version.
- **API:** `GET /api/settings/meta`, `POST /api/settings/preview`, `PUT /api/settings` with `{settings, reason, confirm_looser}` (409 `looser_needs_confirm`), `GET /api/settings/history` in versions.
- Also changed: GUI screens → Settings points to the new section; Settings, secrets and persistence and the API table mention versions; Build order (v0.3) lists item 025.
- The kickoff sets the range and direction of every setting; the spec only says that each has one.

## 2026-11-21 · build PR #51

Backend half: settings now save as numbered versions.

- Every numeric setting has a range (422 with "must be 0.1 to 2" style messages); cross-field rules for risk and rules.
- `settings_directions.py`: one tighter/looser map that fails closed; model and prompt changes and event channels going off always ask once more.
- `settings_versions` + `settings_history.version` (migration 0015); the first boot writes the stored document once as a `migration` version.
- `settings_service.save` enforces the 3–200-character reason and the looser-change confirmation (409 `looser_needs_confirm`); every former `repo.save_settings` caller now saves with its source and reason.
- New endpoints: `GET /api/settings/meta`, `POST /api/settings/preview` (backend Decimal money, saves nothing), paged `GET /api/settings/history`, `POST /api/markets/{m}/activate|deactivate`; `PUT /api/settings` takes `{settings, reason, confirm_looser}` and returns `{version, settings}`; a `settings` WebSocket message carries the new version.
- `PUT /api/notifications` is SMTP-only now; the screen arrives in the next PR.

## 2026-11-21 · build PR #51 (screen)

Same PR, second half: the sectioned Settings screen.

- `/settings/<section>` in five nav groups with a grouped Section select on phones; the last section is remembered.
- `frontend/src/pages/settings/`: meta-driven rows (explanation, default, unit, fixed-as-text, Reset on changed rows), Advanced folds, and custom blocks for markets, sources/keys, SMTP, shadows, schedule, history and the paper account.
- Find a setting and Changed from default filter across sections (per-section counts in the nav).
- Save bar: unsaved count, old → new with tighter/looser tags from the preview, required reason, Discard, "Save as v<n>"; preview range errors disable Save; leaving with a draft asks.
- 409 `looser_needs_confirm` opens the confirm dialog and resends with `confirm_looser`; History lists versions with reasons; "What these limits mean today" comes from the preview summary.
- Tests: `src/pages/Settings.test.tsx` (draft across sections, filters, save disabled on preview error, 409 confirm, leave guard) and `src/pages/settings/rows.test.ts` (every meta key has exactly one row); e2e `settings-sections.spec.ts`, rewritten `settings-save.spec.ts`, updated `limit-orders.spec.ts`; screenshots in `docs/screenshots/025/` at 1440 and 390 px.
