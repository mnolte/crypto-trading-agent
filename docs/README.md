# Trading Agent v0.1 — docs for the implementer

```
docs/
  README.md           this file: how to use the spec
  SPEC.md             the build spec, the single source of truth for behaviour
  DEVIN_KICKOFF.md    kickoff messages for items 001–013 (archive)
  CHANGES.md          what changed in the spec, newest first
  RESEARCH.md         the strategy research findings
  kickoffs/           the kickoff for each work item from 014 (pr-<n>.md up to 015, then NNN-type-slug.md)
  work-items.md       the naming convention and the register of work items
backend/prompts/
  decision.v1.md      the system prompt from the spec, ready to use
design/               the visual reference (see design/DESIGN.md)
```

## Rules

1. **SPEC.md wins on behaviour, the design wins on look and wording.** If they
   disagree, follow the spec and list the difference in the PR under
   "Design differences".
2. **Do not guess.** When the spec is silent or ambiguous on something that changes
   behaviour (a threshold, an order of checks, a default), stop and ask in the PR or
   session instead of inventing a rule. Small implementation details (file names inside
   a module, helper functions) are yours to decide.
3. **Numbers in the spec are defaults.** Every threshold lives in config or Settings, as
   the spec says; never hard-code them in logic.
4. **Money is computed in Python with Decimal.** The model never computes money, and the
   frontend never does arithmetic on money.
5. **One work item per PR.** Build plan items go in the build order; each PR builds only
   what its item lists, with its tests passing. Do not start the next item in the same PR.
   Use the map below for which spec sections a build plan item needs; newer items name
   their sections in the kickoff.
6. **Tests come from the spec.** Every rule in "Risk gates", "Rules engine", "OpenAI
   decision layer" (post-checks) and "Testing and acceptance criteria" gets a test.
7. **Keep `backend/prompts/decision.v1.md` unchanged.** A prompt change is a new file
   (`decision.v2.md`) and a new prompt version, never an edit.
8. **Work items have a number and a name.** Every feature, fix, analysis or ops change
   with a kickoff is a work item `NNN-type-slug` (for example `099-fix-stop-retry`). The
   number is used in branches, PR titles, commits, screenshots and changelog headings.
   `docs/work-items.md` defines the format and holds the register. Items 001–020 are build
   plan items 1–20; "PR n" in the spec means item n, not GitHub PR #n.

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
| 16 Positions & orders | v0.3 → Positions & orders; Market data and paper broker (paper broker); Risk gates (exits) |
| 17 Markets screen | v0.3 → Markets screen; Market data and paper broker (supported markets) |
| 18 Strategies screen | v0.3 → Strategies screen; User features → Shadow strategies; v0.2 → Step 3 (promotion); docs/RESEARCH.md (static BTC + cash benchmark) |
| 19 Backtest | v0.3 → Backtest; The agent cycle; Risk gates; Outcome tracking; docs/RESEARCH.md (method) |
| 20 Sign-in and access | v0.3 → Sign-in and access; v0.3 → Logging, errors and the Logs screen → Error pages and API errors (the 401 and 403 rows); Data model and API (security rules) |

Every PR also checks the "Testing and acceptance criteria" items that belong to it.

## PR description template

```
Title: [NNN] type: Title. Branch: type/NNN-slug (docs/work-items.md).

## What this PR builds
Work item: NNN-type-slug (kickoff docs/kickoffs/…)
<what was built, in a few bullets>

## Spec sections covered
<section names>

## Tests
<what was added; all passing>

## Screenshots (UI PRs)
<1440 × 900 and 390 × 844, next to the matching design/screenshots/*.png>

## Design differences
<none, or each difference and why>

## Open questions
<anything the spec did not decide>
```

## When the spec changes

Spec and design changes arrive as pull requests with the `spec-change` label, each with
an entry in `docs/CHANGES.md` or `design/CHANGES.md`. Claude makes them on a
`spec/NNN-slug` branch; Marc reviews and merges them. Each item's kickoff is a file in
`docs/kickoffs/`, so Devin is started with one line: "Build
docs/kickoffs/NNN-type-slug.md." Treat each entry as a task: update the code and tests it affects, and mention the entry in the PR.
