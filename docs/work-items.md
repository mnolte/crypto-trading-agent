# Work items

Every piece of work that gets a kickoff — a feature, a fix, an analysis, an ops change — is a **work item** with a number and a name. This file defines the name, how a number is claimed, where kickoffs go and how they are laid out, and holds the register up to item 023.

**One file per work item** (decided 19 Nov, item 024). From item 024 on, an item's register fields and change notes are in its own file, `docs/changes/NNN-type-slug.md`, with its design notes in `design/changes/` under the same name (format: `docs/changes/README.md`). The table below is not extended; rows up to 023 are still updated here until those items are done. `python3 scripts/work_items.py` prints the table together with the newer items. The reason: every PR added its row at the same place in this file and its entry at the top of the change logs, so two open PRs always conflicted.

## The name

```
NNN-type-slug        for example   099-fix-stop-retry
```

The examples use 099 as a made-up number.

- **NNN.** Three digits, one sequence for all types, never reused. Items 001–020 are build plan items 1–20 (`docs/SPEC.md` → Build order, Build order (v0.2), Build order (v0.3)).
- **type:**
  - `feat`: new behaviour or a new screen;
  - `fix`: behaviour that is wrong against the spec, or a bug;
  - `analysis`: a read-only report, no code change;
  - `ops`: deployment, CI, tooling, dependencies, performance of the build or tests;
  - `spec`: a spec or design change that needs no build (a clarification, a rule decided in review).
- **slug.** Kebab-case, 2–5 words: what the item does, never when it was made. No dates. A version number only when the thing itself is versioned (`decision-v3`).
- **Title.** The same words in plain English, for the issue and PR titles: "099 · fix · Stop retry".
- **Build plan items with an old kickoff name** (`pr-15.md`) or no kickoff yet take their slug from the register title, for example `feat/015-error-handling-logs`.

## Claiming a number

A number is claimed by **opening its issue first** (decided 19 Nov, item 024), before the branch, the kickoff or the item file. The issue title is `[NNN] Title`; NNN is one above the highest `[NNN]` at the start of any issue or PR title, open or closed:

```bash
gh api "repos/mnolte/trading-agent/issues?state=all&per_page=100" --jq '.[].title' \
  | grep -oE '^\[[0-9]{3}\]' | sort | tail -1
```

GitHub lists pull requests among issues, so this covers both. The issue lists what must be true to close it and names its kickoff. If two items still end up with the same number, the one whose issue was opened later takes the next free number and says so in its issue; that is the only renumbering.

## Where the name goes

| Thing | Format | Example |
| --- | --- | --- |
| Issue | `[NNN] Title`, opened first | `[099] Stop retry` |
| Kickoff | `docs/kickoffs/NNN-type-slug.md`, first line `# NNN · type · Title`; `docs/kickoffs/archive/` from the first build PR | `docs/kickoffs/099-fix-stop-retry.md` |
| Spec branch (Claude) | `spec/NNN-slug` | `spec/099-stop-retry` |
| Build branch (Devin) | `type/NNN-slug` | `fix/099-stop-retry` |
| PR title | `[NNN] type: Title` | `[099] fix: Stop retry` |
| Spec PR title | `[NNN] spec: Title` | `[099] spec: Stop retry` |
| Follow-up PR | same item number, "(follow-up)" | `[099] fix: Stop retry (follow-up)` |
| Commits | start with `[NNN]` | `[099] Retry the stop check once after a stream gap` |
| Change file | `docs/changes/NNN-type-slug.md`, design notes in `design/changes/` | `docs/changes/099-fix-stop-retry.md` |
| Section in the change file | `## YYYY-MM-DD · spec PR` or `· build PR #n` | `## 2026-11-20 · build PR #60` |
| Screenshots | `docs/screenshots/NNN/` | `docs/screenshots/099/` |
| Analysis report | `docs/analysis/NNN-slug.md`, date in its first line | `docs/analysis/099-stop-retry.md` |
| Build plan (SPEC) | item number = work item number | `99. **Stop retry.**` |
| Start Devin | one line | "Build docs/kickoffs/099-fix-stop-retry.md." |

- **Item numbers and GitHub PR numbers never mix.** An item is always written with three digits (`099`, `[099]`, "item 099"), a GitHub PR always with `#` (`#19`).
- The spec and older docs say "PR 14" for build plan item 14. That means item 014, not GitHub #14 (item 014 was built in #19).
- Screenshots of build plan items up to 020 go in `docs/screenshots/pr-<n>/`, as their kickoffs say; from item 021 in `docs/screenshots/NNN/`.
- **One item, one build PR where possible.** An urgent part can be split off as its own item.
- **Small fixes found during a review** of item NNN go into a follow-up PR of that item. They get their own number only when they need their own kickoff.
- **Dependabot and other automatic PRs** keep their own names and get no number.
- **Issues close themselves.** The (last) build PR says `Closes #<issue>`, earlier ones `Part of #<issue>`, a spec PR `Refs #<issue>` (or `Closes` when the spec PR completes a spec-only item). A PR closes only issues it resolves: `AGENTS.md` → How to work.

## Statuses

`planned` (spec PR open), `ready` (spec merged, not started), `building` (build PR open or more PRs to come; kickoff in `archive/`), `merged` (kickoff in `archive/`), `dropped` (with the reason in the item's file; kickoff in `archive/`). The table up to 023 also uses `paused` (held by a decision in `docs/SPEC.md` → Decisions).

## Kickoffs move to the archive with the build

`docs/kickoffs/` holds only kickoffs nobody has started building. The PR that builds a kickoff moves it to `docs/kickoffs/archive/` in the same PR as the code, with `git mv`, same file name, content unchanged:

- one build PR: that PR moves it and sets the item's status to `merged` in its file (the file reaches `main` only with the merge, so `main` is right);
- several build PRs: the first one moves it and sets `building`; the later ones read it from `archive/`, and the last sets `merged`;
- a `spec` or `analysis` item without a build: the PR that completes it;
- a `dropped` item's kickoff moves too, with the reason in its file.

An unchanged move into `archive/` needs no `spec change` label (the gate checks that the file is identical and keeps its name); a move with any edit, or out of the archive, does. If a kickoff must change before it is built, change it in a spec PR first; once merged and started, it is never edited. Old paths in `docs/CHANGES.md`, the table below and code comments are not rewritten: a kickoff that isn't in `docs/kickoffs/` is in `docs/kickoffs/archive/`. `archive/DEVIN_KICKOFF.md` holds items 001–013.

## Kickoff layout

A kickoff is written for Devin to follow without asking. In this order:

1. **The first line** `# NNN · type · Title`, then "Kickoff for Devin. Start the session with: "Build docs/kickoffs/NNN-type-slug.md."".
2. **Scope paragraph:** what the item is for, the spec sections and design files it builds, and its issue.
3. **A fenced `text` block** Devin follows:
   - the goal, and "Start from the latest main" (with any PRs that must be merged first);
   - names: branch, PR title, commits, screenshots folder, the item's file to update;
   - **Read first:** the item's change files, the spec sections, the design files, the `AGENTS.md` hard rules that apply;
   - **PRs:** how many, and which says `Part of #n` and which `Closes #n`; the first moves this kickoff to `docs/kickoffs/archive/`;
   - **Steps,** numbered and concrete: file, function, field, exact text, config key with its default and range; one commit per step where that helps;
   - **Tests** per case, each rule or gate with a pass and a fail case; no network, each test its own database, injected time; "proven in CI, not on the laptop";
   - **Screenshots:** which screens, 1440 × 900 and 390 × 844, in `docs/screenshots/NNN/`;
   - **Rules:** what not to do (the hard rules that matter here, files not to touch);
   - **Done when:** CI green on the last push, the PR merges cleanly, every point of the issue met and listed under "Issues closed", the item's file updated, the kickoff archived.
4. **Why:** the reason for the item, in a few sentences.
5. **Assumptions** made while writing it, each named.
6. **Open questions** for Marc. An answer Marc gives goes into the kickoff before it is merged.

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
| 015 | feat | Error handling, logging and the Logs screen | `pr-15.md` | #18, #20 | #22 | merged |
| 016 | feat | Positions & orders | — | #18 | — | ready |
| 017 | feat | Markets screen | — | #18 | — | ready |
| 018 | feat | Strategies screen | — | #18 | — | ready |
| 019 | feat | Backtest | — | #18 | — | ready |
| 020 | feat | Sign-in and access | — | #18, #20 | — | ready |
| 021 | spec | Naming convention for work items | — | #21 | — | merged |
| 022 | ops | CI hardening | — | — | #23 | merged |
| 023 | spec | Logs in every sidebar | — | #34 | — | planned |

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
