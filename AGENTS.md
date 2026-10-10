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
| The model's system prompt | `backend/prompts/decision.v1.md` |
| What changed since your last PR | `docs/CHANGES.md`, `design/CHANGES.md` |

The spec wins on behaviour; the design wins on look and wording. If they disagree,
follow the spec and list the difference in the PR under "Design differences".

## How to work

1. **One build step per PR**, in the order of the spec's "Build order". Read only the
   sections `docs/README.md` lists for that step. Do not start the next step.
2. **Ask instead of guessing** when the spec is silent or ambiguous about anything that
   changes behaviour: a threshold, an order of checks, a default, a state transition.
   Put the question in the PR or session. Naming, file structure inside a module and
   helper functions are yours to decide.
3. **Every rule gets a test.** Risk gates pass and fail at their boundaries; post-checks,
   Rules conditions, regime labels and outcome labels each have a test. Acceptance items
   in the spec that belong to your step must pass.
4. **Use the PR template** in `docs/README.md`. UI PRs attach Playwright screenshots at
   1440 and 1100 px wide next to the matching `design/screenshots/*.png`.

## Commands

```bash
make dev        # backend (Uvicorn with reload) + Vite dev server
make run        # build the UI, serve everything on 127.0.0.1:8000
make test       # pytest + Vitest (+ Playwright where present)
make ci         # the same checks as the CI jobs (backend, frontend, api-types, e2e)
uv run ruff check . && uv run mypy .     # backend lint and types
cd frontend && corepack pnpm lint && corepack pnpm typecheck
```

Frontend commands run from inside `frontend/` via `corepack pnpm` (the pnpm
version is pinned in `frontend/package.json`).

Run `make test` and the lint/type checks before every commit. Do not mark a PR ready
with failing checks.

## Hard rules

Never break these, even if a task seems to ask for it:

- **Paper only.** No real orders, no exchange trading endpoints, no exchange API keys.
  The Live switch stays disabled.
- **Money in Python, as Decimal.** Cash, fees, P&L, sizing and R/R are computed in the
  backend with `Decimal`; floats only inside indicator maths. The model never computes
  money, and the frontend never does arithmetic on money (Decimals travel as strings).
- **Risk limits live in code and config.** Every threshold comes from config or
  Settings, never a literal in logic. The model's output can never bypass a gate.
- **Any failure means HOLD, never a trade.** Invalid model output, failed post-checks,
  stale or partial data, input-check failures and API errors all end as HOLD or SKIP
  with the reason recorded.
- **One pair per model call.** A brief contains only that pair's data plus the
  market-wide context the spec allows; no other pair's prices, sizes or P&L.
- **Prompts are versioned files.** Never edit `backend/prompts/decision.v1.md` or any
  prompt version in use; a change is a new file (`decision.v2.md`) and a new version.
  Model parameters (temperature 0, max output tokens) belong to the prompt version.
- **Store model calls exactly.** The request as sent and the raw response as returned
  are stored byte-for-byte for the LLM inspector.
- **Secrets never leak.** API keys and the SMTP password live in the OS keyring (or a
  git-ignored `.env`). They never appear in the database, logs, briefs, exports,
  API responses (only "set" and the last 4 characters) or test fixtures.
- **Local only.** Bind to 127.0.0.1, require the `X-Local-Token` header on every write,
  accept WebSocket connections only from the app's own Origin, reject foreign Host
  headers, send no CORS headers.
- **No real network calls in tests.** Mock OpenAI and Bitvavo (respx, mocked SDK); use
  the simulated market and a fake clock for engine tests.
- **Times in UTC** for candles, storage and logic; Europe/Amsterdam only for display and
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
- Edit `docs/SPEC.md`, `design/` or prompt files; ask Marc to change them.
- Never delete `data/` or any database, backup or log outside a test's own temporary
  folder. Tests and smoke runs use a temporary `TA_DATA_DIR` and `TA_DB_PATH`.
- Never kill processes by name (no `pkill -f vite`, `pkill -f uvicorn`). Start
  servers in the background, keep their PIDs, and stop only those PIDs. Other
  projects run on this machine.
