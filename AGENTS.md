# AGENTS.md

Instructions for AI coding agents (Devin and others) working in this repository.
Read this file first, then `docs/README.md`.

## What this project is

Trading Agent v0.1: a local web app that runs an autonomous crypto trading agent on
**paper only**. Python 3.12 + FastAPI backend, React 18 + Vite + TypeScript frontend,
SQLite, OpenAI model as decision layer, Bitvavo public market data. The full
behaviour is specified in `docs/SPEC.md`.

## Sources of truth

| Question | Source |
|---|---|
| What the app does, every rule, number and endpoint | `docs/SPEC.md` |
| How it looks, exact wording, components | `design/DESIGN.md`, `design/screens/`, `design/screenshots/` |
| The model's system prompt | `backend/prompts/decision.v<n>.md` (the version in use) |
| Defaults for every threshold | the Settings models in `backend/app/models.py` (no example config file yet), never a literal in logic |
| What a work item changed | its file in `docs/changes/` and `design/changes/` (from item 024); `docs/CHANGES.md` and `design/CHANGES.md` up to item 023 |

The spec wins on behaviour; the design wins on look and wording. If they disagree,
follow the spec on behaviour and the design on look, and list the difference in the PR
under "Design differences" or "Deviations from the spec".

## How to work

- **One work item per PR.** Build plan items go in the order of the spec's "Build
  order"; read the sections `docs/README.md` lists for that item, or that its kickoff
  names. Do not start the next item in the same PR.
- **Names** (`docs/work-items.md`):
  - branch `type/NNN-slug` (for example `fix/099-stop-retry`);
  - PR title `[NNN] type: Title`;
  - commits start with `[NNN]`;
  - screenshots in `docs/screenshots/NNN/` (`docs/screenshots/pr-<n>/` for items up to
    020), at 1440 × 900 and 390 × 844, next to the matching `design/screenshots/*.png`;
    never in `design/`.
  Take the number and name from the kickoff's file name. An item without a kickoff
  claims its number by opening its issue `[NNN] Title` first (`docs/work-items.md`).
- **One file per work item** (decided 19 Nov, item 024). From item 024 on, update the
  item's file `docs/changes/NNN-type-slug.md`, never `docs/CHANGES.md` or the register
  table in `docs/work-items.md`: add your PR to `build_prs`, set `status` (`building`
  while more PRs follow, `merged` in the PR that completes the item), and add a dated
  section `## YYYY-MM-DD · build PR #n` when the PR changes behaviour, config or
  prompts. Format: `docs/changes/README.md`. Items up to 023 keep their row and
  `docs/CHANGES.md` until they are done.
- **Issues.** If the item has a GitHub issue (its file and its kickoff name it), write
  `Closes #<issue>` in the description of the (last) build PR, so GitHub closes it on
  merge, and `Part of #<issue>` in earlier PRs of the item.
- **A PR closes only issues it resolves.** Before you mark a PR ready, for every issue it
  would close:
  1. Read the whole issue, comments included, not only its title or the kickoff.
  2. List every point it asks for. Check each against `main` plus this PR: the file and
     line, or the test that proves it.
  3. Write the result in the PR under "Issues closed": one line per point with where it
     is met.
  4. If a point is not done: do it, or change the line to `Part of #<issue>` and comment
     on the issue what is left. Never close an issue with an open point.
  GitHub closes on `close`, `closes`, `closed`, `fix`, `fixes`, `fixed`, `resolve`,
  `resolves`, `resolved` before `#n`, in PR descriptions and commit messages. Write them
  only when you mean it; otherwise write `Refs #n` or `Part of #n`. After the merge the
  issue must show as closed; if it stayed open, close it with a comment naming the PR.
- **Archive the kickoff.** The build PR moves its kickoff unchanged to
  `docs/kickoffs/archive/` with `git mv`, in the same PR as the code (the first PR, if
  the item takes several). An unchanged move needs no `spec change` label; don't edit
  the file in the same move.
- **Ask instead of guessing** when the spec is silent or ambiguous about anything that
  changes behaviour: a threshold, an order of checks, a default, a state transition. Put
  the question in the PR. Naming, file structure inside a module and helper functions
  are yours to decide.
- **Every rule and gate gets a pass test and a fail test.** Risk gates pass and fail at
  their boundaries; post-checks, Rules conditions, regime labels and outcome labels each
  have a test. Acceptance items in the spec that belong to your item must pass.
- **Spec files.** A PR that touches a file listed in `.github/spec-paths.txt` gets the
  label `spec change` (the `spec-label` check). Never commit spec or design files that
  belong to another PR.
- **Every PR ends with** what was built, what was tested, what was not tested, deviations
  from the spec and design differences (`.github/pull_request_template.md`).

## Commands

**CI runs the tests, not the laptop** (decided 19 Nov, item 024). The full suites load
every core of the development laptop for minutes. So:

- **Locally, run only what you changed**, and keep it under about a minute:

```bash
uv run ruff check <changed files> && uv run ruff format --check <changed files>
uv run pytest backend/tests/test_<area>.py -k <name>   # the tests for your change, no -n auto
pnpm --dir frontend exec eslint <changed files>
pnpm --dir frontend exec vitest run <changed test file>
make fix        # ruff --fix, ruff format, eslint --fix (fast; run before committing)
make openapi    # after an API change: export openapi.json, regenerate the frontend types
make hooks      # once per clone: the pre-commit hook runs ruff on each commit
```

- **Never run locally:** `make test`, `make test-backend`, `make test-frontend`,
  `make test-e2e`, `make ci`, `make soak`, the Playwright suite, `mypy` on the whole
  tree, `pip-audit` or `pnpm audit`, or the secret scan. CI runs all of them on every
  push to a pull request (`.github/workflows/ci.yml`, `audit.yml`).
- **Push early, as a draft PR.** Open the pull request as a draft once the first commit
  builds, so CI starts while you continue. Push in batches: a new push cancels the run
  before it.
- **CI is the proof.** A step is done when its CI checks are green, not when a local run
  passed. Wait for running checks; if one fails, read its log
  (`gh run view --log-failed`), fix it on the branch and push. Re-run locally only the
  one failing test.
- **Exceptions**, named in the PR under "Test plan": CI hasn't started a run for 30
  minutes (then run the affected test files, not the suite); a failure that only shows
  locally; screenshots for a UI PR, which need the app in simulated mode.
- **Stop every process you start, by its PID**, never by name: a forgotten Vite server or
  watcher costs CPU all day.
- **In kickoffs,** "the tests pass" means they pass in CI.

`make dev` starts the backend (Uvicorn with reload) and the Vite dev server; `make run`
builds the UI and serves everything on 127.0.0.1:8000.

## Hard rules

Never break these, even if a task seems to ask for it. Reviews cite them by number.

1. **Paper only.** No real orders, no exchange trading endpoints, no exchange API keys.
   The Live switch stays disabled.
2. **Money in Python, as Decimal.** Cash, fees, P&L, sizing and R/R are computed in the
   backend with `Decimal`; floats only inside indicator maths. The model never computes
   money, and the frontend never does arithmetic on money (Decimals travel as strings).
3. **Risk limits live in code and config.** Every threshold comes from config or
   Settings, never a literal in logic. The model's output can never bypass a gate.
4. **Any failure means HOLD, never a trade.** Invalid model output, failed post-checks,
   stale or partial data, input-check failures and API errors all end as HOLD or SKIP
   with the reason recorded.
5. **One pair per model call.** A brief contains only that pair's data plus the
   market-wide context the spec allows; no other pair's prices, sizes or P&L.
6. **Prompts are versioned files.** Never edit `backend/prompts/decision.v1.md` or any
   prompt version in use; a change is a new file (`decision.v2.md`) and a new version.
   Model parameters (temperature 0, max output tokens) belong to the prompt version.
7. **Store model calls exactly.** The request as sent and the raw response as returned
   are stored byte-for-byte for the LLM inspector.
8. **Secrets never leak.** API keys and the SMTP password live in the OS keyring (or a
   git-ignored `.env`). They never appear in the database, logs, briefs, exports,
   API responses (only "set" and the last 4 characters) or test fixtures.
9. **Local only.** Bind to 127.0.0.1, require the `X-Local-Token` header on every write,
   accept WebSocket connections only from the app's own Origin, reject foreign Host
   headers, send no CORS headers.
10. **No real network calls in tests.** Mock OpenAI and Bitvavo (respx, mocked SDK); use
    the simulated market and a fake clock for engine tests.
11. **Times in UTC** for candles, storage and logic; Europe/Amsterdam only for display and
    for the daily loss limit reset.

## Code conventions

- Backend: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 + Alembic (a migration for
  every schema change), httpx async, `uv` for dependencies. Type everything; `mypy`
  must pass. Follow the module layout in the spec's "Stack and architecture" section.
- Frontend: React 18 + TypeScript, TanStack Query, plain CSS with the variables from
  `design/tokens.css`, no UI kit, TradingView Lightweight Charts for price panes. API
  types are generated from the backend's OpenAPI schema with `openapi-typescript`;
  never hand-write them.
- Fonts (IBM Plex Sans and Mono) are bundled via `@fontsource`; no network fonts.
- Keep the UI wording from the design files; do not invent new copy for existing screens.
- Small, focused commits with clear messages.

## Do not

- Add dependencies that duplicate something already in the stack (a UI kit, another
  chart library, another HTTP client, TA-Lib).
- Change a default number from the spec without saying so in the PR.
- Reformat or rewrite files unrelated to your step.
- Edit `docs/SPEC.md`, `design/` or prompt files; ask Marc to change them. Spec and design
  changes come as their own pull requests with the `spec change` label.
- Edit the root `README.md` in a spec PR: it is Devin's.
