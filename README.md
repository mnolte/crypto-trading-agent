# Trading Agent

An autonomous crypto trading agent that trades on **paper only**. Every 15 minutes it
reads live Bitvavo EUR prices, computes signals in code, asks an OpenAI model for a
decision when the rules see a possible setup, checks that decision against hard risk
limits, and fills it on a simulated €10,000 account. A local web app shows what the
agent sees, decides and holds, and lets you pause it, veto trades or close everything.

> **Status: v0.1 in development.** No real orders are ever placed. There are no
> exchange API keys, and the Live switch is disabled. Nothing in this project is
> financial advice.

## What it does

- Trades Bitvavo EUR markets on paper: BTC, ETH, SOL, XRP, ADA, LINK, AVAX, DOGE and
  LTC by default, long-only spot, on the 1-hour timeframe with 4-hour and daily context.
- Uses a deterministic rules engine as a pre-filter, baseline and fallback, and an
  OpenAI model (gpt-4o-mini during development) as the decision layer.
- Enforces every risk limit in code: 1% risk per trade, 2.5% total open risk, a −3%
  daily loss limit, a −8% kill switch, correlation limits, a shock brake and more.
- Records what price did after every decision, so decisions are judged by results.
- Lets you inspect every model call: the exact input, the raw output and every check.
- Explains itself: a "why so quiet?" line, morning briefings, weekly reviews, an Ask
  panel, a What-if simulator, Insights and a live-readiness checklist.

## Requirements

- macOS or Linux
- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for the backend)
- Node.js 20 or newer and [pnpm](https://pnpm.io/) for the frontend
- An OpenAI API key (optional: without one the agent runs on its rules engine)

## Quick start

```bash
git clone <this repo> trading-agent
cd trading-agent
make run        # builds the UI; the app serves on http://127.0.0.1:8000
```

The agent always starts **paused**. Open the app, check the Dashboard, then press
**Resume**. To add your OpenAI key, go to **Settings → Data sources and API keys**; the
key is stored in your operating system's keyring, never in the database or logs.

For development:

```bash
make dev        # backend with reload + Vite dev server (proxies /api and /ws)
make test       # backend and frontend tests
```

## Configuration

Settings you change while the app runs (markets, risk limits, model, notifications) are
edited in the **Settings** screen and stored in the database.

Settings for where the app runs come from environment variables. Copy `.env.example`
to `.env`, which git ignores:

| Variable | Default | Meaning |
|---|---|---|
| `TA_HOST` | `127.0.0.1` | Address the API binds to. Keep it local in v0.1. |
| `TA_PORT` | `8000` | Port for the app |
| `TA_DB_PATH` | `data/agent.db` | SQLite database |
| `TA_DATA_DIR` | `data/` | Daily backups and other local data |
| `OPENAI_API_KEY` | – | Fallback when the key is not in the keyring |

Keys for other data sources follow the same pattern, for example `COINGECKO_API_KEY`.

## Project layout

```
backend/            FastAPI app, agent engine, rules, risk gates, paper broker
  prompts/          versioned system prompts for the model (decision.v1.md, …)
frontend/           React + Vite + TypeScript app
docs/
  SPEC.md           the build spec, source of truth for behaviour
  README.md         how to work with the spec, PR plan and template
  DEVIN_KICKOFF.md  messages that start each build step
  CHANGES.md        spec change log
design/
  DESIGN.md         the visual reference: rules, components, screens
  screens/          the 12 design screens as HTML
  screenshots/      one PNG per screen
  tokens.css        colours, type and spacing
  CHANGES.md        design change log
AGENTS.md           instructions for AI coding agents working in this repo
```

## Safety

- Paper trading only. Real orders, exchange keys, leverage, shorting and futures are
  out of scope for v0.1.
- The API listens on 127.0.0.1 only, and every write needs a per-session local token.
- The agent restarts paused and never replays missed cycles.
- The SQLite database is backed up daily to `data/backups/` (last 14 copies kept).

## How it is built

The project is built in eight steps, one pull request each: skeleton, market data,
paper engine, API and GUI, OpenAI decider, execution modes and polish, understanding
and safety, and evidence. See the "Build order" section of
[docs/SPEC.md](docs/SPEC.md) and the PR plan in [docs/README.md](docs/README.md).
