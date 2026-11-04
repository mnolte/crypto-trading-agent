# Work items

Every piece of work that gets a kickoff — a feature, a fix, an analysis, an ops change — is a **work item** with a number and a name. This file is the register: one row per item, in number order. Claude adds the row when the spec PR is opened; whoever opens or merges a PR for the item updates its PR and status columns. This file is not spec-owned (it is not a protected path in CI), so Devin can update it without the `spec-change` label.

## The name

```
NNN-type-slug        for example   099-fix-stop-retry
```

The examples use 099 as a made-up number.

- **NNN.** Three digits, one sequence for all types, never reused or renumbered. It continues the build plan: items 001–020 are build plan items 1–20 (`docs/SPEC.md` → Build order, Build order (v0.2), Build order (v0.3)). The next free number is the row after the last one below.
- **type:**
  - `feat`: new behaviour or a new screen;
  - `fix`: behaviour that is wrong against the spec, or a bug;
  - `analysis`: a read-only report, no code change;
  - `ops`: deployment, CI, tooling, dependencies, performance of the build or tests;
  - `spec`: a spec or design change that needs no build (a clarification, a rule decided in review).
- **slug.** Kebab-case, 2–5 words: what the item does, never when it was made. No dates. A version number only when the thing itself is versioned (`decision-v3`).
- **Title.** The same words in plain English, for PR titles and changelog headings: "099 · fix · Stop retry".
- **Build plan items with an old kickoff name** (`pr-15.md`) or no kickoff yet take their slug from the register title, for example `feat/015-error-handling-logs`. A new kickoff for items 016–020 uses the new file name.

## Where the name goes

| Thing | Format | Example |
| --- | --- | --- |
| Kickoff | `docs/kickoffs/NNN-type-slug.md`, first line `# NNN · type · Title` | `docs/kickoffs/099-fix-stop-retry.md` |
| Spec branch (Claude) | `spec/NNN-slug` | `spec/099-stop-retry` |
| Build branch (Devin) | `type/NNN-slug` | `fix/099-stop-retry` |
| PR title | `[NNN] type: Title` | `[099] fix: Stop retry` |
| Spec PR title | `[NNN] spec: Title` | `[099] spec: Stop retry` |
| Follow-up PR | same item number, "(follow-up)" | `[099] fix: Stop retry (follow-up)` |
| Commits | start with `[NNN]` | `[099] Retry the stop check once after a stream gap` |
| Screenshots | `docs/screenshots/NNN/` | `docs/screenshots/099/` |
| Analysis report | `docs/analysis/NNN-slug.md`, date in its first line | `docs/analysis/099-stop-retry.md` |
| Changelog heading | `## YYYY-MM-DD — [NNN] Title` | `## 2026-11-05 — [099] Stop retry` |
| Build plan (SPEC) | item number = work item number | `99. **Stop retry.**` |
| Start Devin | one line | "Build docs/kickoffs/099-fix-stop-retry.md." |

- **Item numbers and GitHub PR numbers never mix.** An item is always written with three digits (`099`, `[099]`, "item 099"), a GitHub PR always with `#` (`#19`).
- The spec and older docs say "PR 14" for build plan item 14. That means item 014, not GitHub #14 (item 014 was built in #19).
- Screenshots of build plan items up to 020 stay in `docs/screenshots/pr-<n>/`, as their kickoffs say; from item 021 they go in `docs/screenshots/NNN/`.
- **One item, one build PR where possible.** An urgent part can be split off as its own item.
- **Small fixes found during a review** of item NNN go into a follow-up PR of that item. They get their own number only when they need their own kickoff.
- **Dependabot and other automatic PRs** keep their own names and get no number.

## Register

| Nr | Type | Title | Kickoff | Spec PRs | Build PRs | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 001 | feat | Skeleton | `DEVIN_KICKOFF.md` | — | #1 | merged |
| 002 | feat | Market data | `DEVIN_KICKOFF.md` | — | #7 | merged |
| 003 | feat | Paper engine | `DEVIN_KICKOFF.md` | — | #8 | merged |
| 004 | feat | API and GUI | `DEVIN_KICKOFF.md` | — | #9 | merged |
| 005 | feat | OpenAI decider | `DEVIN_KICKOFF.md` | — | #10 | merged |
| 006 | feat | Execution modes and polish | `DEVIN_KICKOFF.md` | — | #11 | merged |
| 007 | feat | Understanding and safety | `DEVIN_KICKOFF.md` | — | #12 | merged |
| 008 | feat | Evidence | `DEVIN_KICKOFF.md` | — | #13 | merged |
| 009 | feat | Market context collector | `DEVIN_KICKOFF.md` | — | #15 | merged |
| 010 | feat | Sentiment block | — | — | — | paused (Decisions, 23 Oct) |
| 011 | feat | Macro calendar | — | — | — | paused (Decisions, 23 Oct) |
| 012 | feat | Limit entries | `DEVIN_KICKOFF.md` | — | #17 | merged |
| 013 | feat | Decision quality (prompt v2) | `DEVIN_KICKOFF.md` | — | #17 | merged |
| 014 | feat | Shell and Dashboard | `pr-14.md` | #18 | #19 | merged |
| 015 | feat | Error handling, logging and the Logs screen | `pr-15.md` | #18, #20 | — | ready |
| 016 | feat | Positions & orders | — | #18 | — | ready |
| 017 | feat | Markets screen | — | #18 | — | ready |
| 018 | feat | Strategies screen | — | #18 | — | ready |
| 019 | feat | Backtest | — | #18 | — | ready |
| 020 | feat | Sign-in and access | — | #18, #20 | — | ready |
| 021 | spec | Naming convention for work items | — | #21 | — | planned |

Status is one of `planned` (spec PR open), `ready` (spec merged, not started), `building` (build PR open), `merged`, `paused` (spec written, held by a decision in `docs/SPEC.md` → Decisions, with the date in the row) or `dropped` (with the reason in the row).

- Spec PRs: before #18, spec changes went straight to `main` or came inside build PRs, so items 001–013 show none.
- Items 012 and 013 were built together in #17 (its commits include "Prompt v2: versioned briefs, trade_math …").
- Kickoff `DEVIN_KICKOFF.md` means the archive's generic "Every later PR" message.

### Before the convention

Merged work outside the build plan, kept under its old names. None of it has a kickoff file in this repository; the kickoff, if any, was given in the Devin session. The old names stay as they are, so links keep working.

| Kickoff | What | Spec PRs | Build PRs |
| --- | --- | --- | --- |
| — | CI: backend, frontend, api-types, e2e, guard and secrets jobs | — | #2, #3, #6 |
| — | Failing markets stay in the cycle; daily history off-by-one | — | #14 |
| — | Signal-claims check: normalise cited names before matching | — | #16 |
| — | Trading path audit fixes (the "fix PR" in Build order (v0.3)) | — | none: pushed to `main` as 54908f8, ee7fcab and 7a3f813 (3–4 Nov) |
