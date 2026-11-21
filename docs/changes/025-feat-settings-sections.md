---
item: 025
type: feat
title: Settings sections
status: ready
issue: 44
kickoff: 025-feat-settings-sections.md
spec_prs: []
build_prs: []
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
