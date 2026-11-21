# Trading Agent v0.1 — docs for the implementer

```
docs/
  README.md           this file: how to use the spec
  SPEC.md             the build spec, the single source of truth for behaviour
  RESEARCH.md         the strategy research findings
  work-items.md       work item names, claiming a number, the kickoff layout, the register up to 023
  changes/            one file per work item from 024: register fields and change notes
  CHANGES.md          what changed in the spec up to item 023, newest first (not extended)
  kickoffs/           kickoffs not yet started (NNN-type-slug.md)
  kickoffs/archive/   kickoffs being built or done (DEVIN_KICKOFF.md for 001–013, pr-14.md, pr-15.md, …)
backend/prompts/
  decision.v1.md      the system prompt from the spec, ready to use
design/               the visual reference (see design/DESIGN.md); design notes per item in design/changes/
```

## Rules

1. **The spec wins on behaviour, the design on look and wording.** `design/DESIGN.md`
   explains the design files. If they disagree, follow the spec on behaviour and the
   design on look, and list the difference in the PR under "Design differences".
2. **Ask, don't guess.** When the spec is silent or ambiguous on something that changes
   behaviour (a threshold, an order of checks, a default, what happens on an error), it
   is a question in the PR, not a decision in code. Small implementation details (file
   names inside a module, helper functions) are yours to decide.
3. **Thresholds live in config.** Every number in the spec is a default in the Settings
   models (`backend/app/models.py`), changeable in Settings. Code reads settings; it
   never repeats the number.
4. **One work item per PR.** Build plan items go in the build order; each PR builds only
   what its item lists. Use the map below for which spec sections a build plan item
   needs; newer items name their sections in the kickoff.
5. **Changes are logged in the item's own file.** Behaviour, spec and design changes get
   a dated section in `docs/changes/NNN-type-slug.md`, and in `design/changes/` for
   design (from item 024; `docs/changes/README.md`). `docs/CHANGES.md` and
   `design/CHANGES.md` hold the history up to item 023. One file per item, so open pull
   requests don't conflict over the log.
6. **The defaults are the end state, but `main` starts at every commit.** A setting whose
   code isn't merged yet goes in the kickoff, not in the Settings models: the app must
   start from `main` with its own defaults at every commit.
7. **Spec changes are labelled.** A pull request that touches a file in
   `.github/spec-paths.txt` (the spec, the agent rules, kickoffs, prompts, the env
   contract, the design) carries the label `spec change`, and CI fails without it. Spec,
   rule, prompt and env changes also need a changed item file in `docs/changes/`; design
   screen changes one in `design/changes/`.
8. **Spec work arrives as pull requests.** Claude makes spec and design changes on a
   `spec/NNN-slug` branch and opens a pull request with the `spec change` label; Marc
   reviews and merges it. Each item's kickoff is a file in `docs/kickoffs/`, so Devin is
   started with one line: "Build docs/kickoffs/NNN-type-slug.md." The build PR moves it to
   `docs/kickoffs/archive/`, so `docs/kickoffs/` lists only work not yet started. The
   root `README.md` is Devin's; spec PRs don't touch it.
9. **Work items have a number and a name.** Every feature, fix, analysis or ops change
   with a kickoff is a work item `NNN-type-slug` (for example `099-fix-stop-retry`). The
   number is used in the issue, branches, PR titles, commits, the item's file and
   screenshots. A number is claimed by opening its issue `[NNN] Title` first.
   `docs/work-items.md` defines the format; `python3 scripts/work_items.py` prints the
   register. Items 001–020 are build plan items 1–20; "PR n" in the spec means item n,
   not GitHub PR #n.
10. **Money is computed in Python with Decimal.** The model never computes money, and the
    frontend never does arithmetic on money.
11. **Tests come from the spec.** Every rule in "Risk gates", "Rules engine", "OpenAI
    decision layer" (post-checks) and "Testing and acceptance criteria" gets a pass and a
    fail test. CI runs them (`AGENTS.md` → Commands).
12. **Prompts are versioned.** `backend/prompts/decision.v1.md` and any version in use
    stay unchanged; a prompt change is a new file (`decision.v2.md`) and a new version.

## Which spec sections each PR needs

| PR | Read these sections of SPEC.md |
|---|---|
| 1 Skeleton | Stack and architecture; Data model and API (security rules, session token); Settings, secrets and persistence (lock file); design/DESIGN.md |
| 2 Market data | Indicator definitions (incl. reads and regime); Market data and paper broker (market data, supported markets, simulated market); Data model and API (source registry) |
| 3 Paper engine | The agent cycle; Rules engine; Risk gates (all gates, sizing, exits, kill switch); Market data and paper broker (paper broker); Testing (engine tests) |
| 4 API and GUI | GUI screens (Dashboard, Decision detail, Indicators, Performance, Settings); Data model and API (the endpoints these screens use, snapshot, WebSocket) |
| 5 OpenAI decider | OpenAI decision layer (configuration, brief, output schema, post-checks, prompt, logging); GUI screens → LLM inspector |
| 6 Execution modes and polish | GUI screens → Dashboard (pending approval); Outcome tracking; Settings, secrets and persistence (notifications, operations, restart and sleep) |
| 7 Understanding and safety | User features → Understanding the agent, Trust and safety |
| 8 Evidence | User features → Improving with evidence |
| 9 Market context collector | v0.2: positioning data → Step 1; Settings, secrets and persistence (data sources) |
| 10 Sentiment block | v0.2 → Step 2 and Risk flags (news flag); OpenAI decision layer (brief, post-checks, prompt versions); User features → Shadow strategies, Insights |
| 11 Macro calendar | v0.2 → Risk flags (macro calendar); Risk gates; User features → What-if simulator |
| 12 Limit entries | v0.2 → Limit entries, tested as Shadow B; Risk gates (fee gate); Market data and paper broker (paper broker); Outcome tracking; User features → Shadow strategies |
| 13 Decision quality (prompt v2) | v0.2 → Prompt v2: decision quality; OpenAI decision layer (brief, output schema, post-checks, logging); GUI screens → LLM inspector (Re-run); backend/prompts/decision.v2.md |
| 14 Shell and Dashboard | v0.3 → Shell and Dashboard; GUI screens → Dashboard; design/DESIGN.md (The shell, Interaction rules, Phone width) and design/CHANGES.md entry "Aligned with Stock Trading Agent" |
| 15 Error handling, logging and Logs screen | v0.3 → Logging, errors and the Logs screen (whole subsection, incl. the failure catalogue); v0.3 → Shell and Dashboard → Needs you; Settings, secrets and persistence (Operations); User features → Live-readiness checklist; The agent cycle and Market data and paper broker (the retry rules); design/DESIGN.md → Logs screen and Screens (`Logs.dc.html`, `Errors.dc.html`); docs/kickoffs/pr-15.md |
| 16 Positions & orders | v0.3 → Positions & orders; Market data and paper broker (paper broker); Risk gates (exits); v0.2 → Limit entries; design/DESIGN.md → Positions & orders screen (`Positions.dc.html`); docs/kickoffs/016-feat-positions-orders.md |
| 17 Markets screen | v0.3 → Markets screen; Market data and paper broker (supported markets) |
| 18 Strategies screen | v0.3 → Strategies screen; User features → Shadow strategies; v0.2 → Step 3 (promotion); docs/RESEARCH.md (static BTC + cash benchmark) |
| 19 Backtest | v0.3 → Backtest; The agent cycle; Risk gates; Outcome tracking; docs/RESEARCH.md (method) |
| 20 Sign-in and access | v0.3 → Sign-in and access; v0.3 → Logging, errors and the Logs screen → Error pages and API errors (the 401 and 403 rows); Data model and API (security rules) |

Every PR also checks the "Testing and acceptance criteria" items that belong to it.

## PR description

Use `.github/pull_request_template.md` (GitHub fills it in): Summary, Issues closed, Spec
files changed, Test plan, Not tested, Deviations from the spec, Design differences. UI
PRs add screenshots at 1440 × 900 and 390 × 844 next to the matching
`design/screenshots/*.png`.

## When the spec changes

Spec and design changes arrive as pull requests with the `spec change` label (CI check
`spec-label`; the spec-owned paths are in `.github/spec-paths.txt`), each with a dated
section in the item's file in `docs/changes/` or `design/changes/`. Claude makes them on
a `spec/NNN-slug` branch; Marc reviews and merges them. Treat each section as a task:
update the code and tests it affects, and mention it in the PR.
