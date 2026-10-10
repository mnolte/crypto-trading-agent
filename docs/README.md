# Trading Agent v0.1 — docs for the implementer

```
docs/
  README.md           this file: how to use the spec
  SPEC.md             the build spec, the single source of truth for behaviour
  DEVIN_KICKOFF.md    the message to start each PR with
  CHANGES.md          what changed in the spec, newest first
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
5. **One PR at a time, in the build order.** Each PR builds only what its step lists, with
   its tests passing. Do not start the next step in the same PR.
6. **Tests come from the spec.** Every rule in "Risk gates", "Rules engine", "OpenAI
   decision layer" (post-checks) and "Testing and acceptance criteria" gets a test.
7. **Keep `backend/prompts/decision.v1.md` unchanged.** A prompt change is a new file
   (`decision.v2.md`) and a new prompt version, never an edit.

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

Every PR also checks the "Testing and acceptance criteria" items that belong to it.

## PR description template

```
## What this PR builds
<step from the build order>

## Spec sections covered
<section names>

## Tests
<what was added; all passing>

## Screenshots (UI PRs)
<1440 px and 1100 px, next to the matching design/screenshots/*.png>

## Design differences
<none, or each difference and why>

## Open questions
<anything the spec did not decide>
```

## When the spec changes

Marc updates SPEC.md and adds an entry to CHANGES.md. Treat each entry as a task:
update the code and tests it affects, and mention the entry in the PR.
