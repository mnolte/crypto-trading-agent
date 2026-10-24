# Trading Agent v0.1 — Build Spec

Version: 10 Oct 2026 · Owner: Marc Nolte · Source of truth: this file (exported from the spec doc)

## Overview

v0.1 is a local web app, with a Python backend and a React frontend, that runs an autonomous crypto trading agent on paper. Every 15 minutes it reads live Bitvavo prices, computes signals in code, asks an OpenAI model for a decision when the rules see a possible setup or a position is open, checks that decision against hard risk limits, and fills it on a simulated account. The GUI shows what the agent sees, decides and holds, and lets Marc pause it, veto trades or flatten everything.

This document specifies the trading agent product. Devin implements it; the last sections give Devin the build order and acceptance tests.

**Goals**

- Trade a configurable set of Bitvavo EUR markets on paper (default nine: BTC, ETH, SOL, XRP, ADA, LINK, AVAX, DOGE and LTC, all against EUR), long-only spot, trading the 1-hour timeframe with 4-hour and daily context, checked every 15 minutes. Holding periods are hours to a few days. All prices, balances, fees and P&L are in euros; the only conversion is model cost, which OpenAI bills in USD.
- Use an OpenAI model as the decision layer, with every decision explained and logged.
- Enforce risk limits in code so the model can never exceed them.
- Give Marc full control from the GUI: pause, run now, approve or veto, stop and flatten.
- Produce a clean record of decisions and paper results to judge whether live trading is worth it, including what price did after every decision, taken or not.

**Non-goals for v0.1**

- Real orders or exchange API keys. The Live switch is shown but disabled.
- Shorting, leverage, futures or margin.
- Fear & Greed, news sentiment and on-chain flows. The input schema leaves room for them in v0.2.
- Hosting on a server. v0.1 runs on Marc's machine; the backend is built so it can move to a server later.

## Stack and architecture

A FastAPI backend in Python runs the agent and owns all state. A React frontend in the browser talks to it over REST plus one WebSocket. Both run on Marc's machine in v0.1, started with one command.

| Layer | Choice |
| --- | --- |
| Backend | Python 3.12, FastAPI, Uvicorn; the agent's asyncio scheduler runs in the same process |
| Indicators | numpy + pandas, no TA-Lib, so install stays simple |
| LLM | Official `openai` Python SDK, Responses API with Structured Outputs built from a Pydantic model |
| HTTP client | httpx (async) for Bitvavo |
| Validation | Pydantic v2 for API payloads, settings, model output and Bitvavo responses |
| Storage | SQLite via SQLAlchemy 2.0, migrations with Alembic |
| Secrets | `OPENAI_API_KEY` from the OS keyring (`keyring`) or a git-ignored `.env`; never in SQLite or logs; every external source's key is stored the same way |
| Frontend | React 18 + Vite + TypeScript, TanStack Query for REST, plain CSS tokens, no UI kit; TradingView Lightweight Charts (Apache 2.0) for the price and indicator panes |
| API types | Frontend types generated from FastAPI's OpenAPI schema with openapi-typescript |
| Tooling | uv for Python, ruff + mypy, pnpm for the frontend |
| Tests | pytest with pytest-asyncio and respx, Vitest, one Playwright end-to-end test |
| Run | `make dev`: Uvicorn with reload + Vite dev server. `make run`: builds the UI, FastAPI serves it on 127.0.0.1:8000 |

**Module layout**

```
backend/
  app/
    main.py               FastAPI app; lifespan starts and stops the engine; serves built UI
    api/routes.py         REST endpoints
    api/ws.py             WebSocket snapshot stream
    agent/
      engine.py           scheduler, cycle runner, 30 s exit watcher
      market/bitvavo.py   public candles, ticker, order book
      market/sim.py       seeded simulated market
      signals.py          RSI, EMA, ADX, VWAP, OBV, ATR, levels, structure
      brief.py            per-pair input for the model
      deciders/base.py    Decider protocol
      deciders/openai_decider.py
      deciders/rules.py
      risk.py             gates and sizing
      broker/paper.py     paper account, fills, fees, P&L
      market/universe.py  eligibility check with 3-day buffer, warming up
      outcomes.py         hourly outcome worker
      shadows.py          shadow runners with their own paper accounts
      budget.py           daily call limits and the monthly € cap
    features/
      quiet.py, risk_budget.py, readiness.py, insights.py
      whatif.py           replay with stored fills and Rules stand-ins
      ask.py              Ask panel with read-only tools
      briefings.py        morning briefing and weekly review, with the number check
    notify.py             browser and email alerts, repeat suppression
    jobs.py               scheduled jobs and their catch-up on wake
    glossary.yaml
    models.py             Pydantic: Decision, Position, Settings, Snapshot
    db.py, repo.py        SQLAlchemy tables + repositories
    config.py             settings and secrets loading
  config/models.yaml      model prices per million tokens
  prompts/decision.v1.md  system prompt, versioned
  tests/
frontend/
  src/                    React app
  src/api/schema.d.ts     generated from OpenAPI
Makefile, .env.example
```

Rules that hold everywhere:

- Signals, sizing, R/R and P&L are computed in Python. The model chooses an action and levels; it never computes money.
- Money uses `Decimal` for cash, fees and P\&L; floats only inside indicators.
- Both deciders implement one `Decider` protocol and return the same `Decision` model, so the GUI and risk layer don't care which one ran.
- Each pair is decided in its own call with only its own data, so one pair's inputs never leak into another's decision.
- The API binds to 127.0.0.1 only. Every write endpoint requires an `X-Local-Token` header generated at startup (the UI reads it from GET /api/session, which other sites can call but not read), so other sites open in the browser can't trigger trades.
- Every request must use Host 127.0.0.1 or localhost, which blocks DNS rebinding. The WebSocket accepts only connections whose Origin is the app itself, because browsers don't apply CORS to WebSockets and any open site could otherwise read the snapshot. No CORS headers are sent; in make dev, Vite proxies /api and /ws to the backend so the UI stays same-origin. Exactly one engine instance runs per database; Uvicorn runs with a single worker. Bind address, port, database path and data folder come from environment variables (TA_HOST default 127.0.0.1, TA_PORT 8000, TA_DB_PATH data/agent.db, TA_DATA_DIR data/; listed in .env.example), so the same build can later run on a VPS behind a reverse proxy without code changes.

## The agent cycle

Each cycle runs pair by pair: code gathers data and computes signals, the model proposes, and code checks the proposal before anything fills. The OpenAI model is consulted only when Rules sees a setup or a position is open.

```mermaid
flowchart TD
  A[Cycle starts 45 s after the quarter hour] --> B[Per pair, up to 3 in parallel:<br/>fetch closed candles, order book]
  B --> C{Data fresh?}
  C -- no --> H1[HOLD, mark pair stale]
  C -- yes --> D[signals.py: indicators, reads, levels, regime]
  D --> E{Rules pre-filter:<br/>open position, or C1 + 3 of C2-C6?}
  E -- no --> H2[Rules HOLD stands, no model call]
  E -- yes --> F[Build brief, run input checks]
  F -- check fails --> H3[HOLD, warning]
  F -- ok --> G[OpenAI call, Structured Outputs]
  G --> I{Post-checks:<br/>schema, action, levels, regime rules}
  I -- fail --> H4[HOLD, reason shown]
  I -- ok --> J{Risk gates and sizing}
  J -- fail --> S[SKIP, gate named]
  J -- pass --> K{Execution mode}
  K -- automatic --> L[Paper fill]
  K -- supervised / manual --> M[Pending approval:<br/>gates recomputed at fill]
  M --> L
  L --> N[Log decision, update snapshot, record outcome later]
  W[Exit watcher every 30 s:<br/>stops, targets, breakeven, trailing,<br/>time stop, kill, invalidation re-check] -.-> L
```

Only a proposal that passes both the output checks and every risk gate reaches execution. A separate watcher closes positions on stops, targets or the kill drawdown without waiting for a cycle, the model or approval.

**Timing and time zones**

- Cycles start 45 seconds after each quarter hour (00:45, 15:45, 30:45, 45:45 past the hour; the offset is a setting), so the hourly candle has closed and Bitvavo has published it.
- If the newest closed 1h candle is not there yet, the cycle retries up to 3 times, 10 seconds apart, then holds that pair and marks it stale.
- Up to 3 pairs run in parallel within a cycle, each with only its own data, so nine pairs finish well within the interval; a cycle that is still running when the next is due makes the next one wait rather than overlap.
- Candles, daily bars, indicator windows and stored timestamps use UTC. The GUI shows Europe/Amsterdam time, and the daily loss limit resets at 00:00 Europe/Amsterdam.

## Indicator definitions

Every indicator is computed in `signals.py` on closed candles only, in UTC, exactly as defined here. The brief, the Indicators screen, the Rules engine and the signal-claims check all use these values, so they can never disagree.

| Indicator | Definition |
| --- | --- |
| EMA n | Standard exponential average of closes, seeded with the simple average of the first n closes; valid only after n + 50 bars |
| RSI 14 | Wilder's RSI on closes |
| ATR 14 | Wilder's average true range |
| ADX 14 | Wilder's ADX from +DI and −DI |
| ATR % | ATR ÷ close × 100 |
| ATR percentile | Rank of the current ATR % among the last 30 days of values on the same timeframe (1h: 720 bars, 4h: 180, 1d: 30) |
| VWAP | Rolling 24 bars: Σ(typical price × volume) ÷ Σ volume, typical price = (high + low + close) ÷ 3 |
| Price vs VWAP, vs EMA 200 | (close ÷ reference − 1) × 100 |
| OBV trend | (OBV now − OBV 20 bars ago) ÷ total volume of those 20 bars; range −1 to 1 |
| Volume × average | Volume of the last closed bar ÷ 20-bar simple average volume |
| Swing high / low | A bar whose high (low) is the highest (lowest) of the 3 bars on each side |
| Support and resistance | Swing levels from the last 200 1h bars and 120 4h bars, merged when within 0.3 × ATR(1h); the 3 nearest above and below price |
| Previous day high / low | High and low of the previous UTC day |
| Structure summary | Built from templates in code: consecutive closes above or below EMA 20; count of consecutive higher swing highs or lower swing lows; "range-bound for N bars" when the last N bars stay within 1.5 × ATR(1h) |
| Relative strength vs BTC | Pair return minus BTC-EUR return over 24h and 7d, in % |
| Correlation with BTC | Pearson correlation of 1h log returns over the last 30 days |
| EUR/USD move | Inverse of the USDC-EUR return over 1h and 24h, in % |

**Reads** (bullish / bearish / neutral): RSI above 55 or below 45; price vs VWAP and vs EMA 200 beyond ±0.1%; OBV trend beyond ±0.1; EMA 20 above or below EMA 50; ADX at 25 or more (≥ 25) is labelled trending, below 25 ranging. ATR % and ATR percentile read low below the 30th percentile, normal from the 30th to the 80th, high above the 80th. Volume × average reads high above 1.5, low below 0.7, otherwise normal. The ADX threshold of 25 is one config value, used by these reads, the regime, Rules condition C6 and Insights.

**Regime** (computed in `signals.py` for every pair and for BTC-EUR, stored with every decision):

- **Trend** from the latest closed 4h and 1d bars. *Up*: 4h ADX at least 25, 4h EMA 20 above EMA 50, and daily close above daily EMA 200. *Down*: 4h ADX at least 25, 4h EMA 20 below EMA 50, and daily close below daily EMA 200. *Sideways*: everything else, including 4h ADX under 25 or 4h and daily disagreeing.
- **Volatility** from the 1h ATR percentile: *low* below the 30th, *normal* 30th to 80th, *high* above the 80th.
- **Age**: closed 4h bars since the trend label last changed. A new trend label only takes effect after 2 closed 4h bars in a row agree, so the label doesn't flip on one bar.
- Example label: `up · normal · 14 bars`. The same label is used by the brief, the Rules engine, the Indicators screen, the decision drawer, Insights, the scorecard and What-if.

**History needed:** 1h candles are fetched with `limit=800` (about 33 days) so the 30-day percentile and correlation are complete; 4h and 1d with `limit=300`. If Bitvavo's endpoint caps the limit lower, page with `start` and `end`.

## Rules engine

A deterministic decider in `deciders/rules.py`. It is used three ways: as the pre-filter that decides whether the model is called, as the baseline every model decision is compared with, and as the fallback decider when there is no API key, the budget is spent, or the model fails.

**Conditions** (evaluated on the latest closed 1h bar)

| # | Condition |
| --- | --- |
| C1 | Higher-timeframe trend up: 4h EMA 20 above EMA 50, and daily close above daily EMA 200 |
| C2 | 1h EMA 20 above EMA 50 |
| C3 | 1h close above VWAP |
| C4 | 1h RSI between 45 and 70 |
| C5 | 1h OBV trend above +0.1 |
| C6 | 1h ADX at least 25 (the shared ADX threshold) |

**Pre-filter:** call the model when the pair has an open position, or when C1 holds and at least 3 of C2–C6 hold. Otherwise the Rules decision stands and no call is made.

**Rules BUY** (baseline and fallback): no open position, C1 holds, at least 4 of C2–C6 hold, and price is no more than 1.5 × ATR(1h) above 1h EMA 20, so it doesn't chase an extended move.

- Stop: lowest low of the last 6 1h bars minus 0.2 × ATR(1h), then widened to the fee gate's minimum distance if needed. If the resulting stop is more than 5 × ATR(1h) away, Rules does not BUY (SKIP).
- Target: nearest resistance above entry; if none within 6 × ATR(1h), entry + 2.5R.
- The risk gates then decide as for any BUY.

**Rules SELL** (only when holding, on top of the stop, target, breakeven and time-stop exits): 1h close below 1h EMA 50 with OBV trend below 0, or the 4h EMA 20 crossing below EMA 50.

Regime: Rules only BUYs when the pair's regime is up; in sideways and down it holds. **Confidence:** conditions met ÷ 6. **Reasoning:** a generated sentence listing which conditions held and which failed, so Rules decisions read like model decisions in the GUI.

All thresholds live in config, so the baseline can be tuned without code changes; each Rules decision stores the config version it used.

## OpenAI decision layer

The model gets one pair's market brief and returns a schema-checked decision; code then verifies every number it gave. Any failure means HOLD, never a trade.

**Configuration** (Settings → Decision model)

- Model id: free-text field, default gpt-4o-mini during development. Changing it takes effect next cycle. Prices per million input and output tokens for each model live in config/models.yaml; the cost estimate uses them, and an unknown model shows its cost as unknown instead of guessing.
- Decider: OpenAI (default) or Rules baseline. Rules also runs automatically when no API key is set. The OpenAI model is only called when Rules flags a possible setup or the pair has an open position; otherwise the Rules HOLD stands.
- Model parameters: temperature 0 and max\_output\_tokens 600, written in the header of the prompt file; changing them means a new prompt version, so replays, shadows and the scorecard always compare like with like. Timeout 30 s, one retry on network error or HTTP 5xx/429, no retry on invalid output.
- Daily call budget (default 1,000) for trading decisions, invalidation re-evaluations and replays. When reached, the agent switches to Rules and logs it. Ask has its own limit (100 questions a day), each shadow its own (default 300 calls a day), and briefings and weekly reviews (about 2 calls a day) count only toward the monthly cap. Monthly cost cap (default €10) over every model call: decisions, Ask, briefings, weekly reviews, shadows and replays. At 80% an alert is sent and shadows, Ask and replays pause, while briefings and reviews continue; at 100% decisions also fall back to Rules until the 1st of the next month. Costs are shown in euros, converted from OpenAI's USD prices with the day's rate from the USDC-EUR market; the inspector also shows the USD amount.

**Input: the market brief** (JSON, one pair per call)

| Field | Content |
| --- | --- |
| `pair`, `now`, `price` | Pair, ISO timestamp, last price in EUR |
| `tf.1h`, `tf.4h`, `tf.1d` | Per timeframe: RSI 14, EMA 20 and 50 (plus EMA 200 on 1d), ADX 14, price vs VWAP %, OBV trend, ATR 14 as % of price, ATR percentile vs the last 30 days, volume vs its 20-bar average. Every value comes with its read: bullish, bearish or neutral for RSI, EMA 20 vs 50, VWAP, OBV and EMA 200; trending or ranging for ADX; low, normal or high for ATR and volume |
| `structure` | Up to 3 support and 3 resistance levels from 1h and 4h swings, plus the previous day's high and low, each as % and ATR(1h) distance from price; a one-line pattern summary computed in code, e.g. "higher highs for 6 bars" |
| `market_context` | All computed from Bitvavo data: BTC-EUR 4h and 1d trend reads, 24h change and ATR percentile (volatility regime); relative strength vs BTC over 24h and 7d; ETH/BTC trend from ETH-EUR ÷ BTC-EUR; 30-day correlation of 1h returns with BTC; EUR/USD move over 1h and 24h from the USDC-EUR market, so a move caused by the euro is visible. The BTC brief omits the vs-BTC fields. Each field carries value, read, source and as-of time |
| `liquidity` | Spread %, order-book depth within ±0.5% of price in EUR, 24h volume vs its 30-day average |
| `position` | Null, or qty, entry, stop, target, hours held, unrealised %, best and worst move since entry in R |
| `pair_history` | Last 3 closed trades on this pair with result in R, and hours since the last exit |
| `previous_decision` | Last decision for this pair: action and invalidation only, not its reasoning |
| `account` | Exposure %, today's P&L %, drawdown % |
| `portfolio` | Pairs already held, each with its 30-day correlation to this pair and hours held; no prices, sizes or P&L from other pairs. Plus correlated slots left, whether the risk throttle is on, and whether the shock brake is on |
| `regime` | For the pair and for BTC: trend (up, down, sideways), volatility (low, normal, high) and age in closed 4h bars, all computed in code as defined under Indicator definitions. The BTC brief has one block |
| `limits` | min\_rr, min\_stop\_atr, max\_stop\_atr, min\_stop\_pct (from the fee gate) and max\_age\_s per timeframe (the freshness limits) |
| `time` | Hour in Europe/Amsterdam and weekday |
| `data_age_s` | Seconds since the newest closed candle closed (open timestamp + interval), per timeframe |

No raw candle arrays are sent: patterns are summarised in code, and values use relative units (%, ATR multiples) wherever possible. Sentiment and positioning (funding, open interest, long/short ratio, options data, Fear & Greed) and other external sources come after v0.1; see that section.

**Output schema** (Structured Outputs, `strict: true`)

```json
{
  "action": "BUY | SELL | HOLD",
  "confidence": 0.0,
  "entry": "number | null",
  "stop": "number | null",
  "target": "number | null",
  "reasoning": "max 600 chars, plain language",
  "signals": [{"name": "string", "tf": "1h|4h|1d", "read": "bullish|bearish|neutral|trending|ranging|low|normal|high"}],
  "invalidation": "what would prove this wrong",
  "invalidation_price": "number | null"
}
```

- BUY only when flat; SELL only when holding; anything else is rejected as invalid.
- `entry`, `stop`, `target` and invalidation\_price are typed number | null in the Pydantic model and the strict schema (every field is present; null is allowed). A BUY must give all four; other actions return null for entry, stop and target, and invalidation\_price is null when flat and not buying.

**Post-checks in code** (any failure → HOLD with reason shown in the GUI)

- Output parses against the Pydantic model the schema is generated from.
- On BUY: stop < entry < target, entry within 0.5% of last price, stop distance between 0.5 and 5 × ATR(1h).
- R/R is recomputed by code as (target − entry) / (entry − stop). The model's own claims about R/R are ignored. An R/R below the minimum is not a post-check failure: the Min reward/risk gate turns the BUY into SKIP. If the model's stop is closer than the fee gate's minimum distance, the fee gate turns it into SKIP; code never moves the model's stop. Regime rules are also enforced in code: a BUY while the pair's regime is down (other than BTC-EUR), or, in a sideways regime, a BUY whose entry is more than 1 × ATR(1h) above the nearest support or whose target is above the nearest resistance, becomes HOLD; confidence above its cap (0.7 in high volatility; 0.6 for a BTC-EUR BUY in a down regime) is lowered to the cap and flagged.

**Prompt**

- System prompt lives in `prompts/decision.v1.md`. Each decision stores the prompt version and model id.
- It defines the role (disciplined swing trader, long-only spot), the meaning of every brief field, and the rule to prefer HOLD when signals conflict across timeframes.
- It tells the model that stale data (any `data_age_s` beyond its timeframe's freshness limit) means HOLD.
- No few-shot trades in v0.1, to avoid anchoring on example prices.

**Draft of `prompts/decision.v1.md`** (the system prompt; the brief is sent as the user message)

```markdown
You are the decision layer of a paper-trading agent. You trade spot crypto against EUR, long only, on the 1-hour timeframe with 4-hour and daily context. Typical holding period: hours to a few days.

Each request contains one market brief for one pair, as JSON. Choose one action:
- BUY: only if there is no open position on this pair.
- SELL: only if there is an open position on this pair.
- HOLD: anything else. HOLD is the default; you are not rewarded for trading.

Reading the brief
- Indicators come as {"value", "read"}. Reads are computed in code with fixed thresholds. Use them as given; do not recompute or reinterpret them.
- Distances are in % of price and in multiples of ATR(1h).
- market_context describes BTC and the euro. If BTC's 4h and daily trends are down, require stronger evidence for any non-BTC BUY.
- A null field was unavailable. Never guess its value.
- portfolio lists coins already held and how closely each moves with this one. Code enforces the correlation limit, the shock brake and the risk throttle; mention them in reasoning when they matter, but do not try to work around them.
- If any data_age_s is above its limit in limits, the data is stale: answer HOLD.

BUY only when all of these hold
1. The 4h and daily trends are not down.
2. At least three of these 1h conditions hold: EMA 20 above EMA 50, price above VWAP, OBV bullish, RSI bullish, ADX trending.
3. A stop below a real level (a swing low or support) lies within limits.min_stop_atr to limits.max_stop_atr times ATR(1h), and at least limits.min_stop_pct % below price.
4. The nearest resistance leaves at least limits.min_rr times the risk.
5. Nothing in pair_history or previous_decision argues against it, such as a recent stop-out on this pair.

SELL when the reason for holding no longer holds
- The 1h trend has broken (close below EMA 50 with OBV falling), or the 4h trend has turned down.
- Do not SELL only because price moved against the position; the stop handles that.

Levels
- entry: the current price.
- stop: just beyond the level that would prove the trade wrong, not an arbitrary percentage.
- target: the nearest meaningful resistance, not a hope.
- Code recomputes risk/reward and sizes the trade. Never mention position size or euro amounts.

Regime
- regime gives trend (up, down, sideways), volatility (low, normal, high) and age in 4h bars, for this pair and for BTC. It is computed in code; use it as given.
- up: the BUY rules above apply.
- sideways: BUY only within 1 × ATR(1h) above a support level, with the target at or below the nearest resistance. Never BUY a breakout.
- down: HOLD. The only exception is BTC-EUR when every BUY rule holds; then confidence is at most 0.6.
- high volatility: confidence at most 0.7, and the stop goes beyond the nearest real level, not inside the noise.
- age below 6 bars: the regime just changed; require one more agreeing 1h signal than usual.
- When holding and the pair's regime turns down, say so in reasoning and consider SELL.

Confidence
- 0.5 means no edge. Go above 0.7 only when timeframes and signals clearly agree.

Signals
- List only signals that appear in the brief, with exactly the read the brief gives.

Reasoning
- Plain language, at most 600 characters, referring to fields in the brief.

Invalidation
- One concrete, observable condition that would prove the decision wrong.
- invalidation_price: the price at which that condition is met. On BUY it lies between the stop and entry; while holding, below the current price; when flat and not buying, null. Crossing it makes code ask you again at once.

Never
- Use information that is not in the brief.
- Change your standards because of the account's recent profits or losses.
```

The `limits` field in the brief carries `min_rr`, `min_stop_atr`, `max_stop_atr``, min_stop_pct and max_age_s per timeframe (the freshness limits)`, filled from Settings, so the prompt never hard-codes a number. Prompt changes are made by adding a new versioned file (`decision.v2.md`) and comparing it with replays before switching.

**Logging**

Each call stores the full request exactly as sent (model parameters, system prompt, brief, schema), the raw response exactly as returned, the parsed decision, post-check results, every retry attempt, OpenAI's request id, token counts, latency and cost estimate. The LLM inspector reads these records. The GUI shows today's call count and estimated spend.

## Risk gates

Every BUY passes all gates in `agent/risk.py` before it can execute; a failed gate turns it into SKIP with the gate named. Sizing is done here, never by the model.

| Gate | Default | Applies to | On fail |
| --- | --- | --- | --- |
| Min reward/risk | 2.0 | BUY | SKIP |
| Costs vs risk | Round-trip fees + slippage (about 0.6%) ≤ 30% of the stop distance, so stops at least about 2% away | BUY | SKIP |
| Risk per trade | 1.0% of equity | BUY sizing | Size reduced to fit |
| Total open risk | Σ max(0, entry − stop) × qty (a stop at or above entry counts as zero risk) over all open positions plus the new trade ≤ 2.5% of equity; the pairs move together, so this caps the combined bet | BUY sizing | Size reduced to fit; SKIP if below the market minimum or €10 |
| Max total exposure | 100% of equity: spot only, no leverage, so in practice available cash is the limit, and the 2.5% open-risk cap is the real risk limit | BUY sizing | Size reduced; SKIP if below €10 |
| One position per pair | on | BUY | SKIP |
| Cooldown after stop-out | 6 hours per pair | BUY | SKIP |
| Correlated positions | A BUY is skipped when the pair's 30-day correlation of 1h returns is above 0.8 with 2 or more open positions, so at most 2 closely correlated positions are open at once; BTC counts like any other pair | BUY | SKIP, naming the correlated positions |
| Shock brake | BTC-EUR moves more than 4% up or down within any 60-minute window (checked on the 30 s ticker), or any active pair's last closed 1h candle has a range above 3 × ATR(1h) | BUY | No new entries on any pair for 2 hours from the last trigger; exits keep working; alert |
| Risk throttle | After 3 losing trades in a row, or drawdown beyond 4% from peak: risk per trade 0.5% instead of 1%. Each trigger lifts on its own: the losing streak after the next winning trade, the drawdown once drawdown from the same peak is 2% or less. Risk is back to 1% when neither trigger is active | BUY sizing | Size halved; shown on the risk budget card |
| Cash available incl. fees | — | BUY | Size reduced; SKIP if below €10 |
| Data freshness | Newest closed candle at most one interval + 15 min old, measured from the candle's close (Bitvavo timestamps are open times, so close = timestamp + interval) (1h: 75 min; 4h: 4h 15m; 1d: 24h 15m) | all | HOLD, warn in GUI |
| Daily loss limit | −3% of day-start equity | BUY | No new entries until 00:00 Europe/Amsterdam |
| Kill drawdown | −8% from peak equity | all | Flatten all, Killed state, alert; see below |

**Sizing:** qty = (equity × risk %) ÷ (entry − stop), then capped by exposure, then by cash, rounded down to the pair's Bitvavo quantity precision.

**Exits the agent always takes, without the model or approval:**

- Stop hit: last price ≤ stop.
- Target hit: last price ≥ target.
- Breakeven: once price ≥ entry + 1R, stop moves to entry + fees. Trailing stop (setting, off by default): once price ≥ entry + 2R, the stop follows at the highest 1h close since entry minus 2 × ATR(1h), and never moves down. Try it in What-if or as a shadow before switching it on. Invalidation: if price crosses a position's stored invalidation price, the model is asked about that pair at once instead of at the next cycle; this is a re-evaluation, not an exit, and counts toward the daily call budget. It fires at most once per stored invalidation price: the new decision's invalidation price replaces it, and if the model returns none, nothing more fires until a later decision sets one. Time stop: a position still open after 5 days is closed at market.
- Kill drawdown or Stop & flatten button.

These are checked every 30 s on the ticker price, not only once per cycle, so a stop is never up to 15 minutes late.

**After the kill switch fires**

- The agent closes every position at market, enters the Killed state and sends an alert. Exits keep being watched; no new trades are possible.
- Resume is disabled until Marc acknowledges the kill in a dialog that shows the drawdown, the positions closed and when it happened.
- Acknowledging resets peak equity to current equity, so drawdown is measured afresh, logs the event, and moves the agent to Paused. Resume then works as normal.
- A paper-account reset also clears the Killed state.

## Market data and paper broker

Prices come from Bitvavo's public REST API with no key; fills happen on a local paper account that charges realistic fees.

**Market data** (`agent/market/bitvavo.py`)

- Base URL `https://api.bitvavo.com/v2` ([Bitvavo Node SDK docs](https://www.npmjs.com/package/bitvavo)).
- Candles: `GET /{market}/candles?interval=1h&limit=800` every cycle; `interval=4h` and `interval=1d` with limit=300 once per hour (enough history for EMA 200 on 1d). Each candle is `[timestamp, open, high, low, close, volume]` with prices as strings. Sort ascending by timestamp; don't assume order.
- Order book: `GET /{market}/book?depth=50` once per cycle, for spread and depth. USDC-EUR 1h candles are also fetched every cycle as the EUR/USD proxy; ETH/BTC is computed from the ETH-EUR and BTC-EUR candles already loaded.
- Ticker: `GET /ticker/price?market={market}` every 30 s for stops, targets and equity.
- The API is weight-limited per IP per minute. Read the rate-limit headers, back off on 429, and keep requests far below the limit.
- Drop the still-forming last candle before computing signals. Market rules per pair (minimum order size, price and amount precision) are loaded from GET /markets at startup and refreshed daily; sizing and paper fills always respect them.
- On any fetch error: mark the pair stale, skip it this cycle, show it in the GUI. Never decide on partial data.

**Supported markets** (`agent/market/universe.py`)

Any Bitvavo EUR market can be traded if it passes an eligibility check; Marc chooses which eligible markets are active.

| Rule | Threshold |
| --- | --- |
| 30-day average daily volume | at least €1M |
| Average spread over the last 24h | at most 0.15% |
| History | at least 300 daily candles (about 10 months), so the daily EMA 200 and the regime are valid |
| Type | not a stablecoin or fiat-pegged token (USDC, EURC, and similar) |
| Status on Bitvavo | trading, not halted or delisting |

- The check runs at startup and daily at 01:00 UTC, using Bitvavo's `/markets` and `/ticker/24h` plus stored candles. Thresholds live in config. To stop markets flipping in and out, an active market fails only after 3 days in a row below a threshold, or at once when it is 20% beyond it (for example under €0.8M volume); a failed market needs 3 days back within the thresholds to pass again. One alert per change, not per day.
- Default active set: BTC, ETH, SOL, XRP, ADA, LINK, AVAX, DOGE, LTC (all -EUR).
- Activating a market starts a history backfill; it shows Warming up and is not traded until 800 1h candles, 300 4h and 300 1d candles are loaded.
- If an active market fails the check, it shows a warning and an alert is sent. It stays active until Marc acts, but takes no new entries while failing.
- Deactivating a market stops new entries; an open position on it is still managed by its stop, target, time stop and the model until it closes.
- Each extra active market adds up to 96 model calls a day before the pre-filter; Settings shows the estimate next to the daily budget.

**Simulated market** (`agent/market/sim.py`)

- Same interface as Bitvavo. A seeded random walk with realistic volatility per pair.
- Used by tests and selectable in Settings for offline demos. The GUI shows a SIMULATED badge when active.

**Paper broker** (`agent/broker/paper.py`)

- Starting balance €10,000 (setting), EUR only.
- Market fills at last ticker price ± 0.05% slippage against you.
- Fee 0.25% per side (setting): Bitvavo's entry-tier taker fee for EUR markets, which applies under €100,000 of 30-day volume.
- Tracks cash, positions, realised and unrealised P&L, equity, peak equity and day-start equity.
- "Reset paper account" in Settings clears cash, positions, trades, equity and the Killed state after a confirm dialog and starts a new account number. Decisions, model calls, verdicts and outcomes are kept, tagged with the account they belonged to; Performance and Readiness count from the reset, and Insights can include earlier accounts. A separate "Delete all data" button wipes everything except settings and keys, after typing DELETE to confirm.

## GUI screens

The app has ten views (nine screens plus the Decision drawer; the design's 12 files add the Ask panel and a States and dialogs board), built to match the [Trading Agent GUI design canvas](https://claude.ai/artifact/Vorz1Hc5NGu547i6h1ViGU): Dashboard, Decision detail, Indicators, Performance, Insights, What-if, Reviews, Readiness, LLM inspector and Settings. All screens are in the design, plus the Ask panel, linked through a shared collapsible sidebar: logo, an Ask button, then the screens grouped as Monitor (Dashboard, Indicators, Performance), Learn (Insights, What-if, Reviews, LLM inspector) and Control (Readiness, Settings), each with an icon (Decision detail opens as a drawer), agent status and a collapse button. Collapsed, it shrinks from 232 px to a 72 px icon rail with tooltips; the choice is remembered per browser. Each screen keeps a slim top bar with its title and page actions. The separate States and dialogs board in the design shows: first run, stale data, Killed, the kill acknowledgement, confirm dialogs for Stop & flatten, reset and re-run, the supervised-mode approval queue with two pending trades, a browser notification, the monthly cost cap reached, the Ask limit reached, a What-if run in progress, Insights before there is enough data, and Delete all data. Sample numbers in the design are placeholders; every value comes from the agent snapshot.

**Visual system** (from the design)

- Dark only in v0.1. Ground #0B0E13, panels #12161D, lines #232A35, text #E7EAEE, muted #98A2AF.
- Accent amber #F2B94B for agent state. Up #3DD68C, down #FF6369; always paired with a sign or arrow, never colour alone.
- IBM Plex Sans for UI, IBM Plex Mono for every number. Bundle both fonts; no network fonts.
- Minimum hit target 44 px. Layout works from 1100 px wide; the browser tab title shows agent status and today's P&L.

**Dashboard**

| Area | Contents and behaviour |
| --- | --- |
| Header | Status pill (Running / Paused / Killed / Error), Paper–Live switch (Live disabled with tooltip), Pause/Resume, Run cycle now, Stop & flatten (confirm dialog), Settings |
| KPI strip | Equity and % since start, today's P&L, exposure bar vs cap, drawdown bar vs kill level, next-cycle countdown and cycle number, today's model calls and est. cost |
| Chart | Pair selector with 24h change: tabs for the first 6 active pairs, the rest in a searchable More menu; 1h / 4h / 1d toggle; candlestick or line from cached candles; buy/sell markers from trades; open position's entry, stop and target as horizontal lines |
| Latest decision | Action badge, confidence, reasoning, signal chips, R/R, size, stop/target, decider + model id, Open full reasoning |
| Pending approval | Shown in supervised or manual mode: one card per pending trade, each with its own veto countdown, Approve and Veto. At fill, R/R, size and every gate are recomputed with the fill price, so two approvals together can never break the caps. A pending trade expires at that pair's next cycle, or earlier if price moves more than 0.5% from the planned entry |
| Positions | Pair, size, entry, last, stop, target, unrealised €, and a Close button per row |
| Activity | Newest first: cycle starts, decisions, skips with gate name, fills, exits, errors. Filter by pair |

**Decision detail** (drawer from the right)

- Header: cycle number, time, pair, decider, model id, prompt version, and the regime chips for the pair and BTC at that moment.
- Verdict with confidence bar; each risk gate with pass / warn / fail and its value.
- Signal table for 1h, 4h and 1d; the model's reasoning and invalidation text; entry, stop, target.
- Collapsible raw brief and raw model response (JSON) for prompt auditing.
- Prev / next to step through history; any past decision opens from the activity log.

**Indicators**

A screen in the main navigation to inspect every indicator the agent computes, per pair and timeframe, using exactly the values the brief uses. The frontend never recomputes an indicator; everything comes from `signals.py` through the API.

- **Selectors:** the same pair selector as the Dashboard and a 1h / 4h / 1d toggle. The window shows the last 300 bars, with zoom and pan.
- **Price pane:** candles with EMA 20, 50 and 200, VWAP, support and resistance levels, and the previous day's high and low as overlays. Each overlay can be switched on or off. Trades and decisions are marked on the bars where they happened.
- **Indicator panes below**, sharing the time axis: RSI with its 45 / 55 read bands, ADX, OBV, ATR as % of price with its 30-day percentile, and volume against its 20-bar average.
- **Crosshair:** hovering a bar shows every indicator's value and read at that bar.
- **Current values table:** one row per indicator with its value and read for 1h, 4h and 1d side by side, and the threshold that produced the read.
- **Context panel:** the structure summary text, levels with their distance in % and ATR, the BTC market context, and liquidity (spread, depth, volume). Regime: a strip above the price pane shows the pair's and BTC's current regime with its age, and a thin band under the candles colours each bar by its trend regime, so you can see when the label changed and why.
- **Data health:** bar count, age of the newest closed candle against its freshness limit, and any missing bars in the window.
- **As the model saw it:** "Open in Indicators" from a decision or an LLM inspector call shows the screen frozen at that moment, from the stored brief and candles, with the decision bar highlighted. A banner shows the timestamp and a button returns to live.
- **Export:** CSV of candles plus every indicator series for the visible window, to check the maths against another charting tool.

**Performance**

A screen in the main navigation that judges the paper account as a whole, answering "is this worth going live". The outcome scorecard judges decisions; this screen judges money.

- **Period:** 7 days, 30 days, since start, or a custom range.
- **Equity curve** from `equity_snapshots`, with a benchmark line: the account's starting balance held in BTC from the same start, so the agent is compared with simply buying and holding.
- **Drawdown chart** under the equity curve, with the kill level marked.
- **Totals:** return %, benchmark return %, max drawdown, number of trades, win rate, average R, profit factor, fees paid in €, and time in market %.
- **Per pair:** trades, realised P&L in €, average R, fees.
- **Closed trades table:** pair, opened and closed time, quantity, entry and exit price, fees, realised € and R, exit reason (target, stop, breakeven, time stop, model SELL, kill, manual), with a link to the opening decision.

**Settings**

- Markets: a table of Bitvavo EUR markets with 30-day volume, spread, history, eligibility and an Active switch, plus search to add one; ineligible markets show the failed rule; a footer shows active count and estimated model calls per day; cycles run every 15 minutes (fixed in v0.1; only the start offset is a setting).
- Execution: Automatic (default), Supervised (veto window 60 s), or Manual approval.
- Risk limits from the Risk gates table, each with validation and its unit.
- Decision model: decider, model id, prompt version, daily call budget, Rules pre-filter switch.
- Data source: Bitvavo live prices or Simulated. Paper account: starting balance, fee %, reset. Data sources and API keys: one row per source (OpenAI, Bitvavo market data, and every external source as it is added), showing what it is used for, whether a key is not needed, optional or required, an on/off switch, a masked key field with Test, Replace and Remove, the last successful fetch, and rate-limit usage. Sources planned for later appear disabled with their phase noted.
- Notifications: channels (browser, email with SMTP settings), a switch per event and channel, and Send test. Save applies on the next cycle. Unsaved changes show a bar with Save and Discard. Model costs: monthly € cap and this month's spend per feature. Learning and explanations: plain-language mode, quiet line, briefing time, weekly review day, Ask questions per day. Shadow strategies: up to 2, each with Compare, Promote (confirm) and Stop. Live readiness: the thresholds, with changes logged. Danger zone: Reset paper account and Delete all data.

**LLM inspector**

The inspector exists to debug the model: see exactly what it was fed, and judge whether its answer was correct for that input. It is a permanent screen in the main navigation, in development and in production, so every live decision stays auditable. In production, Re-run asks for confirmation because it spends a model call.

*Call list* (newest first, new calls appear live over the WebSocket)

| Column | Content |
| --- | --- |
| Time, cycle, pair | When and for which pair the call was made |
| Model, prompt version | Model id and system prompt version used |
| Status | OK, invalid output, timeout, API error, or retried |
| Latency | Milliseconds, including retries |
| Tokens | Input and output token counts from the API response |
| Est. cost | In euros at the day's rate, with the USD amount OpenAI bills |
| Final action | The action after post-checks and risk gates, e.g. BUY → SKIP (R/R gate) |

Filters: pair, status, date, has issues (any input or output check not passing), and your verdict. A "Show skipped" switch adds rows for cycles where the Rules pre-filter decided no call was needed, so gaps in the list are explained.

*Call detail*, two panes side by side

- **Input:** the request exactly as sent: model id and parameters, the full system prompt text, the market brief JSON (pretty-printed, collapsible), and the JSON schema used for Structured Outputs.
- **Output:** the raw response body exactly as returned, unaltered; the parsed decision; each post-check with pass or fail and its reason; token usage; OpenAI's request id; and every retry attempt with its error.
- **Actions:** copy any pane, download the whole call as one JSON file, and open the linked decision in the Decision drawer.

*Input checks* (run on the brief before the call, shown above the Input pane)

- Missing, null or NaN fields, and indicator values outside their valid range (RSI 0–100, ATR > 0, prices > 0).
- Stale data: any timeframe beyond its freshness limit (see Risk gates). Market-context fields have their own freshness limit per source; a missing or stale context field is sent as null with a reason and shown as a warning, not a block.
- Too little history for an indicator: fewer than n + 50 bars for EMA n, or fewer than 3 × the period for RSI, ATR and ADX.
- Cross-pair leak: any price in the brief more than 50% away from the pair's last price, which usually means another pair's data got in.

A failed input check means the call is not sent and the cycle holds; a warning is sent and shown. Either way you can tell bad input from a bad answer.

*Output correctness checks* (shown above the Output pane, each pass / warn / fail with its reason)

| Check | What it catches |
| --- | --- |
| Schema valid | Output doesn't parse into the decision model |
| Action allowed | BUY while holding, or SELL while flat |
| Levels sane | Stop not below entry, target not above, entry more than 0.5% from price, stop outside 0.5–5 × ATR(1h), invalidation price not between stop and entry on BUY, or not below the current price while holding |
| R/R recomputed | R/R from the model's levels below the minimum (shown for information; the action becomes SKIP at the Min reward/risk gate) |
| Signal claims vs input | Each signal the model cites is compared with the brief's actual value, using fixed reads: RSI > 55 bullish, < 45 bearish; price vs VWAP beyond ±0.1%; OBV trend beyond ±0.1; EMA 20 vs EMA 50; ADX trending at 25 or more; ATR low / normal / high at the 30th and 80th percentile; volume high above 1.5× and low below 0.7× its average. A claim that contradicts the input is a warn |
| Stale-data rule | Model traded although the brief marked data stale |
| Regime rule | BUY while the pair's regime is down (other than BTC-EUR), in a sideways regime an entry more than 1 × ATR(1h) above support or a target above resistance, or confidence above its cap (0.7 in high volatility, 0.6 for BTC-EUR in a down regime) |
| Rules baseline | What the Rules decider does on the same brief. Disagreement is shown as information, not as an error |

*Replay*

- "Re-run" sends the stored brief again, with the same or a different model id and prompt version, and shows the new output next to the original with differences highlighted.
- Replays are labelled as replays, never trade, and count toward the daily call budget.

*Your verdict*

- Mark any call Correct or Wrong, with an optional note on what was wrong.
- Verdicts are stored with the call and filterable. "Export labelled calls" downloads them as JSONL, to reuse as a test set when changing the prompt or model.

Nothing is redacted, because the API key is never part of the stored request. The inspector reads only stored records, so opening it never triggers a model call.

## User features

Eleven features that reduce confusion, anxiety and guesswork for the person running the agent. One rule applies to every feature that writes text with a model (Ask, briefing, weekly review): **numbers come from code, the model only writes prose**. Each output is checked so that every number in the text appears in the facts it was given; if not, the text is regenerated once, then replaced by a plain template.

**Understanding the agent**

1. **Ask the agent.** A chat panel opened from the sidebar on every screen. Questions like "Why didn't you buy SOL yesterday?" are answered by an OpenAI model with read-only tools: `get_decisions`, `get_trades`, `get_outcomes`, `get_llm_call`, `get_performance`, `get_indicators_at`, `get_settings`, `get_activity`. Every answer cites the records it used as links (decision, trade, call). It cannot change settings, trade or pause anything; if the records don't answer the question it says so. Further read-only tools: get\_snapshot (risk budget, quiet line, pauses), get\_insights, get\_readiness and get\_glossary, so "What does fee gate mean?" is answered from the glossary. Limited to 100 questions a day (setting) and to the monthly cost cap; one question may use several model calls, each logged in the LLM inspector. Conversations are stored locally and listed inside the panel. The panel slides in from the right over the current screen and receives that screen's context (for example the open decision).
2. **"Why so quiet?" status.** A one-line explanation on the Dashboard, computed in code each cycle, no model: time since the last trade, how many pairs failed which pre-filter condition, how many reached the model and what it decided, and the gates that blocked trades. Active brakes and pauses are added at the end, global ones first: the shock brake, a pause on all entries, then per-coin pauses (for example "Shock brake on until 16:30. All new entries paused until 14:00. DOGE paused for 18h."). Example: "Quiet: 7 of 9 pairs failed the trend filter (4h down). 2 reached the model; both held. Last trade 31h ago."
3. **Plain-language mode.** A `glossary.yaml` with a one-to-two-sentence definition per term (R, ATR, drawdown, flatten, exposure, VWAP, OBV, ADX, pre-filter, fee gate, and so on). Terms in the UI get a dotted underline and a tooltip; there is no separate Glossary page; Ask answers "what does X mean?" from the same file. On by default; switch in Settings.
4. **Daily briefing.** At 08:00 Europe/Amsterdam (default; the time is a setting) the agent writes a 150–250 word note from a summary computed in code: trades opened and closed, P&L vs BTC, open positions and their distance to stop and target, notable SKIPs, alerts, and the 3 pairs closest to a setup. Shown as a card on the Dashboard and sent by email; it replaces the plain daily summary email. If there is no OpenAI key, the model call fails or the monthly cost cap is reached, the computed facts are rendered with the plain template instead: the card and email still appear, labelled with the reason (for example "Plain briefing: no OpenAI key"), and the failure is logged. Weekly reviews follow the same rule.

**Trust and safety**

5. **Live-readiness checklist.** A Readiness screen with criteria that must all pass before the Live switch can be unlocked (live trading itself is a later phase). Defaults, editable in Settings: at least 100 closed trades; at least 30 days of paper trading; return above BTC buy-and-hold after fees over the same period; max drawdown under 6%; average R above +0.2 after fees; profit factor above 1.3; confidence calibration within 10 percentage points (BUYs grouped by stated confidence into 0.5–0.6, 0.6–0.7, 0.7–0.8 and 0.8+; per bucket the gap between average stated confidence and actual win rate; gaps averaged weighted by bucket size, skipping buckets with fewer than 10 BUYs; cannot pass before 50 BUYs); no kill-switch event in the last 30 days; no unresolved errors in the last 7 days (only errors that affected a decision or a fill; see Operations). Each criterion shows its current value, target, pass or fail, and for counts an estimated date at the current pace. Criteria count from the current paper account. Any threshold change is kept in the settings history; a loosened threshold is shown on the screen with its date.
6. **Per-coin pause.** Pause new entries on one coin, or on all coins, for 1 hour, 24 hours, until a date and time, or until resumed. Exits and the model's SELL decisions keep working; pauses expire automatically, show a clock badge on the pair, and are logged. Set from the pair selector, the latest-decision card or the Settings markets table; Ask cannot set it. While a pair's entries are paused and it has no open position, the model is not called for it (shadows included): the Rules engine answers, any BUY ends as SKIP at the Entry pause gate, and no call budget is spent. With an open position the model is called as usual, so SELL decisions keep working. The Entry pause gate is checked again when a pending approval fills.
7. **Risk budget meter.** A Dashboard card that also shows, when active, the risk throttle (0.5% per trade and what lifts it), the shock brake (and when it ends) and the correlated slots left, plus three bars: open risk vs the 2.5% cap, today's loss vs the 3% daily limit, drawdown vs the 8% kill level; plus one line such as "Room for about 2 more full-size trades".

**Improving with evidence**

8. **What-if simulator.** A screen that re-runs a stored period with changed settings: risk gates, Rules thresholds, min R/R, fee gate, max hold, cooldown, total risk cap, correlation limit, shock brake, risk throttle, trailing stop and active pairs. Model decisions are held fixed (no new calls), and so are human choices: a decision that was vetoed, expired or still pending in reality never fills in a scenario. A scenario can block a real fill, or fill a decision that a gate skipped in reality, but never overrides a veto or an expiry; Rules decisions, gates, sizing, fills and outcomes are recomputed from stored candles. When a scenario reaches a pair and cycle the model never saw (looser Rules thresholds, or an added coin), the Rules decision stands in; those trades are marked "Rules stand-in" and the results show how many there are. Fills: a trade that matches a real one uses the stored real fill prices; other exits are checked on 1h candles, stop first when one candle touches both levels. Every real fill and every 30-second price check within 0.5% of a stop or target is stored (exit\_ticks), so a run with unchanged settings reproduces the actual results exactly. Results: equity curve vs what actually happened, changed metrics, and the trades added or removed. A banner explains that the model's answers are held fixed. Scenarios can be saved; "Apply these settings" asks for confirmation.
9. **Insights.** A screen that slices outcomes and trades by pair, hour of day, weekday, regime (the stored trend and volatility label), volatility percentile bucket, confidence bucket, decider and prompt version. Metrics: count, BUY win rate, average R, missed vs avoided. Includes a calibration chart: stated confidence vs actual win rate. A slice needs at least 30 decisions to count as a finding; slices with 10–29 show greyed as "early signal", and slices under 10 are hidden. Weekly reviews and suggestions only use findings. Before there is enough data the screen says how many more decisions are needed.
10. **Shadow strategies.** Up to 2 shadows, each a model id, prompt version and optional settings overrides. A shadow gets the same briefs as live every cycle: built from the same cycle snapshot (candles, indicators, regime, timestamps), with the position and account block taken from the shadow's own paper account, so a shadow holding a position can get SELL answers. Like live, it calls the model when its pre-filter passes or when it holds a position on the pair, and its gates (correlation, open-risk cap, throttle, cooldown) use its own account. It never trades, keeps its own paper account and outcomes, and appears in the scorecard, Insights and What-if. Its account starts flat, with cash equal to the live account's equity at creation; the start equity and creation time are stored, and Compare measures live's return from that same moment. Each shadow has its own daily call limit (default 300) and pauses first when the monthly cost cap reaches 80%: it then makes no new decisions (no Rules stand-in) until the 1st of the next month or until the cap is raised, while the watcher keeps managing its open positions. The pause window is stored, shown in Compare and left out of both sides' return and drawdown comparison. A shadow's settings overrides also apply to its own Rules pre-filter, so it may call the model on cycles where live did not. Its stops, targets and time stops are checked by the same 30-second watcher against its own paper account. Promote replaces the live model or prompt after a confirmation.
11. **Weekly review.** Every Monday 08:00 (default; day and time are settings) a report page and email: the week's metrics vs the previous week and BTC, the 3 best and worst trades with links, patterns in decisions marked Wrong (grouped by which output check fired), insights that changed with enough data, and one evidence-backed suggestion with a "Try in What-if" link. Past reviews are kept on a Reviews screen.

## Outcome tracking

After every decision the agent records what price actually did, so decisions are judged by results rather than by how convincing the reasoning sounded. It covers every decision: BUY, SELL, HOLD, SKIP, and cycles held by the Rules pre-filter.

**What is measured**

| Decision | Reference levels |
| --- | --- |
| BUY (executed) | The real entry, stop and target, and the realised result in R after fees once the trade closes |
| BUY turned SKIP, or BUY rejected by checks | The levels the model proposed |
| HOLD, pre-filter hold | A hypothetical long at the decision price: stop 1.5 × ATR(1h) below, target 2R above |
| SELL | Price change after the exit, to show whether selling was early or right |

For each decision, at 4h, 24h and 72h after it:

- Forward return in %.
- Best and worst move in % and in R (maximum favourable and adverse excursion).
- First touch: target first, stop first, or neither within 72h. Measured on closed 1h candles; when one candle touches both, it counts as stop first. Invalidation: whether price crossed the model's invalidation price within 72h, and what price did in the 24h after the cross, to judge whether those levels were sensible.

**Labels derived from it**

- BUY: win or loss by first touch, or "open" when neither level is touched within 72h; plus realised R for executed trades.
- HOLD or SKIP: "avoided" if the hypothetical stop hit first, "missed" if the target hit first, "flat" otherwise.
- SELL: "good exit" if price is lower after 24h, "early exit" if it is more than 1R higher, "neutral" in between. Here 1R is the position's original risk per unit (entry fill price minus the stop set at entry), never the stop at exit time, which may sit at breakeven or be trailed. "After 24h" is the close of the last closed 1h candle at exit + 24h, compared with the exit fill price.

**How it runs**

- An outcome worker in the backend runs every hour, fills every outcome whose horizon has passed, and backfills after downtime. It is idempotent, and each record stores the method version.
- Bitvavo 1h candles are the source; closed candles never change, so results are reproducible.
- Stored in an `outcomes` table: decision id, horizon, measures, label, computed at, method version.

**Where it shows**

- Decision drawer and LLM inspector: the outcome next to the decision, or "pending until" with the time. Your verdict judges the reasoning; the outcome judges the result; both are shown side by side.
- LLM inspector: filter by outcome label, and a scorecard per decider, model and prompt version: number of decisions, BUY win rate, average R, and missed versus avoided for HOLD and SKIP.
- Exports: outcomes are included in the JSONL and CSV exports, next to the brief, so the data can later train or test a model.

## Data model and API

The backend owns all state and pushes a full `Snapshot` to the UI over the WebSocket after every change; the UI never computes trading values itself.

**Core types** (`app/models.py`)

```python
Action = Literal["BUY", "SELL", "HOLD", "SKIP"]
ExecMode = Literal["auto", "supervised", "manual"]

class Signal(BaseModel):
    name: str
    tf: Literal["1h", "4h", "1d"]
    read: Literal["bullish", "bearish", "neutral", "trending", "ranging", "low", "normal", "high"]

class Gate(BaseModel):
    name: str
    status: Literal["pass", "warn", "fail"]
    value: str

class Plan(BaseModel):
    entry: Decimal; stop: Decimal; target: Decimal
    qty: Decimal; notional: Decimal; rr: Decimal

class Decision(BaseModel):
    id: str; cycle: int; at: datetime; pair: str
    decider: Literal["openai", "rules"]
    model: str | None = None; prompt_version: str | None = None
    action: Action; confidence: float
    reasoning: str; invalidation: str | None = None
    signals: list[Signal]; plan: Plan | None = None; gates: list[Gate]
    status: Literal["executed", "pending", "approved", "vetoed", "expired", "none"]
    execute_at: datetime | None = None; error: str | None = None
    regime: Regime; btc_regime: Regime    # trend, volatility, age_4h
    invalidation_price: Decimal | None = None
    risk_pct_used: Decimal                # 1.0 normally, 0.5 when throttled
    account_id: int; settings_version: int
    shadow_id: str | None = None          # None = live
    stand_in: bool = False                # What-if only: Rules answered for a missing model call

class Position(BaseModel):
    pair: str; qty: Decimal; entry: Decimal; stop: Decimal; target: Decimal
    opened_at: datetime; decision_id: str; last: Decimal; unrealised: Decimal

class Snapshot(BaseModel):
    status: Literal["running", "paused", "killed", "error"]
    mode: Literal["paper"]; source: Literal["bitvavo", "sim"]
    cycle: int; next_run_at: datetime | None; cycle_in_progress: bool
    equity: Decimal; cash: Decimal; start_equity: Decimal; today_pnl: Decimal
    exposure_pct: float; drawdown_pct: float
    prices: dict[str, PriceInfo]          # last, chg_24h_pct, stale
    positions: list[Position]; latest: dict[str, Decision]; pending: list[Decision]
    activity: list[ActivityItem]          # at, kind, pair, text, decision_id
    usage: Usage                          # calls_today, cost_today_eur, cost_month_eur, cap_eur
    account_id: int
    quiet: str                            # the "Why so quiet?" line
    risk_budget: RiskBudget               # open risk, today's loss, drawdown, each with limit; full-size trades left
    paused: list[EntryPause]              # pair or "*", until
    errors_24h: int; errors_open: int
    kill: KillInfo | None                 # set while Killed, for the acknowledgement dialog

class RiskBudget(BaseModel):
    open_risk_pct: Decimal; open_risk_cap_pct: Decimal
    today_loss_pct: Decimal; daily_limit_pct: Decimal
    drawdown_pct: Decimal; kill_pct: Decimal
    full_trades_left: int                 # min(open-risk room, cash room), at a 3% stop
    risk_pct_now: Decimal                 # 1.0, or 0.5 while throttled
    throttle: Throttle | None
    shock_brake_until: datetime | None
    correlated_slots_left: int

class Throttle(BaseModel):
    reasons: list[Literal["streak", "drawdown"]]; lifts_when: str

class KillInfo(BaseModel):
    at: datetime; peak: Decimal; equity: Decimal; drawdown_pct: Decimal
    closed: list[str]                     # pairs closed by the kill

class Regime(BaseModel):
    trend: Literal["up", "down", "sideways"]
    volatility: Literal["low", "normal", "high"]
    age_4h: int

class PriceInfo(BaseModel):
    last: Decimal; chg_24h_pct: Decimal; stale: bool

class ActivityItem(BaseModel):
    at: datetime; kind: str; pair: str | None; text: str; decision_id: str | None

class Usage(BaseModel):
    calls_today: int; daily_budget: int
    cost_today_eur: Decimal; cost_month_eur: Decimal; cap_eur: Decimal

class EntryPause(BaseModel):
    pair: str                             # "*" = all coins
    until: datetime | None
```

The snapshot also carries `quiet` (the "Why so quiet?" line), `risk_budget` (open risk, today's loss and drawdown, each with its limit, plus the number of full-size trades that still fit) and `paused` (per-coin and global entry pauses with their expiry). New tables: `briefings` (daily and weekly, with their computed facts), `ask_conversations`, `whatif_scenarios`, `shadows` and `shadow_decisions`.

Decimals are serialised as strings in JSON; the frontend formats them and never does arithmetic on money.

**SQLite tables:** `settings`, `decisions` (with raw brief and raw response), `trades`, `positions`, `equity_snapshots` (one per cycle), `activity`, `llm_calls`, candles (every closed 1h, 4h and 1d candle per market, read by the Indicators replay, What-if and outcomes), exit\_ticks (prices at real fills and near-stop or near-target checks, for What-if), settings\_history (every change with time, old and new value; decisions store the settings version), accounts (one row per paper account; trades, decisions and outcomes carry account\_id), entry\_pauses, alerts (sent alerts, for the 30-minute repeat rule) and errors (with affected decision or fill, resolved\_at and note).

Outcome tracking adds an `outcomes` table, described in its own section.

**Source registry:** each data source module declares a descriptor: id, name, purpose, key requirement (none, optional or required), whether it is enabled, its freshness limit and rate limit. The Settings page and `GET /api/sources` are built from these descriptors, so adding a source needs no new UI. A required key that is missing disables the source with a clear message instead of failing cycles.

**API** (FastAPI on 127.0.0.1:8000; every POST and PUT needs the `X-Local-Token` header, which the served UI receives at load)

| Endpoint | Effect |
| --- | --- |
| `GET /api/snapshot` | Current snapshot, including quiet status, risk budget and pauses |
| `WS /ws` | Sends two message types: {"type": "snapshot"} after each change, and {"type": "alert"} with event, title, text and link for every alert whose browser channel is on (the frontend shows these with the Notification API). The client reconnects with backoff and refetches the snapshot on reconnect |
| `GET /api/health` | Engine state, last cycle, source status, errors in the last 24h |
| `POST /api/agent/pause`, `/resume` | Stop or restart the scheduler; exits still run while paused; resume is refused while Killed |
| `POST /api/agent/run-now` | Start a cycle now if none is running; 409 if one is |
| `POST /api/agent/flatten` | Close every position at market and pause |
| `POST /api/agent/acknowledge-kill` | Confirms the kill, resets peak equity to current equity, moves to Paused |
| `PUT /api/agent/entry-pause`, `DELETE /api/agent/entry-pause` | Pause or resume new entries on all coins, with an optional `until` |
| `POST /api/positions/{pair}/close` | Close one position at market |
| `POST /api/decisions/{id}/approve`, `/veto` | Resolve one pending decision; R/R, size and gates are recomputed at the fill price; 409 if it has expired |
| `GET /api/markets` | Every Bitvavo EUR market with 30-day volume, spread, history, eligibility, the failed rule if any, active, warming-up and paused state |
| `PUT /api/markets/{market}` | Activate or deactivate a market; activation is refused with the failed rule when ineligible |
| `PUT /api/markets/{market}/pause`, `DELETE /api/markets/{market}/pause` | Pause or resume new entries on one coin, with an optional `until` |
| `GET /api/candles/{pair}?tf=1h` | Cached candles plus trade markers for the dashboard chart |
| `GET /api/indicators/{pair}?tf=1h&bars=300&at=` | Candles plus every indicator series, current values and reads, levels, context and data health. With `at` (a decision or call id) it returns the stored state from that moment |
| `GET /api/indicators/{pair}/export.csv?tf=&from=&to=` | Candles and indicator series as CSV |
| `GET /api/performance?from=&to=` | Equity curve, BTC buy-and-hold benchmark, drawdown series, totals and per-pair figures |
| `GET /api/trades?pair=&from=&to=` | Closed trades with fees, realised € and R, and exit reason |
| `GET /api/readiness` | Every live-readiness criterion with value, target, status and estimated date |
| `GET /api/insights?dimension=&from=&to=&decider=&prompt_version=` | Outcome and trade metrics sliced by one dimension, with sample sizes; `dimension=confidence` returns the calibration data |
| `POST /api/whatif` | Starts a simulation for a period and settings overrides; returns a job id |
| `GET /api/whatif/{id}` | Progress, then equity curves, metric changes and added or removed trades |
| `POST /api/whatif/{id}/save`, `/apply` | Save the scenario, or apply its settings after confirmation |
| `GET /api/shadows`, `POST /api/shadows`, `DELETE /api/shadows/{id}` | List, create (model, prompt version, overrides) or stop a shadow strategy |
| `POST /api/ask` | A question plus optional conversation id; returns the answer with cited records |
| `GET /api/ask/conversations` | Stored conversations |
| `GET /api/briefings?type=daily\|weekly`, `GET /api/briefings/{id}` | Daily briefings and weekly reviews with their computed facts |
| `GET /api/glossary` | All glossary terms and definitions |
| `GET /api/decisions/{id}` | Full decision incl. raw brief, response and outcome |
| `GET /api/decisions?pair=&outcome=&before=` | Paged history for the drawer and activity log |
| `GET /api/outcomes/stats?group=decider,model,prompt_version,shadow&from=&to=` | Scorecard: decisions, BUY win rate, average R, missed vs avoided for HOLD and SKIP |
| `GET /api/llm-calls?pair=&status=&issues=&verdict=&outcome=&before=&skipped=` | Paged LLM call list for the inspector |
| `GET /api/llm-calls/{id}` | One call: request as sent, raw response, parsed decision, input and output checks, attempts, verdict, outcome |
| `GET /api/llm-calls/{id}/download` | The same record as a downloadable JSON file |
| `POST /api/llm-calls/{id}/replay` | Re-sends the stored brief with an optional model id and prompt version; returns the replay record, never trades |
| `PUT /api/llm-calls/{id}/verdict` | Sets Correct or Wrong plus an optional note |
| `GET /api/llm-calls/export.jsonl?verdict=&outcome=` | Labelled calls with outcomes as JSONL |
| `GET /api/settings`, `PUT /api/settings` | Validated by Pydantic; invalid input returns 422 with field errors |
| `GET /api/notifications`, `PUT /api/notifications` | Channels, SMTP settings (password excluded) and per-event switches |
| `POST /api/notifications/test` | Sends a test message to every enabled channel |
| `GET /api/sources` | Every data source with purpose, key requirement, key set (last 4 only), enabled, last success, rate-limit usage |
| `PUT /api/sources/{id}` | Enable or disable a source |
| `PUT /api/secrets/{source}`, `DELETE /api/secrets/{source}` | Store or remove a key in the keyring (also the SMTP password); the secret is never returned |
| `POST /api/secrets/{source}/test` | One minimal request with the stored key; returns ok or the provider's error message |
| `POST /api/paper/reset` | Starts a new paper account (cash, positions, trades, equity, Killed state); keeps decisions, calls, verdicts and outcomes |
| `DELETE /api/data` | Wipes all data except settings and keys; requires {"confirm": "DELETE"} |
| `GET /api/activity?pair=&kind=&before=` | Paged activity log, filterable by pair and kind (e.g. kind=error for the error badge link) |
| `GET /api/whatif` | Saved What-if scenarios with their settings and last results |
| `POST /api/shadows/{id}/promote` | Makes the shadow's model and prompt version live after confirmation; the shadow stops; logged in the settings history |
| `GET /api/ask/conversations/{id}` | One stored conversation with its questions, answers and cited records |
| `GET /api/prompts` | Available prompt versions with their model parameters, for Settings, Re-run and shadows |
| `POST /api/maintenance/prune` | Deletes raw briefs and responses older than 90 days; keeps parsed decisions and outcomes |
| `GET /api/session` | The X-Local-Token for the served UI; no CORS headers, so other sites cannot read it |
| `GET /api/errors, PUT /api/errors/{id}/resolve` | Open and resolved errors; dismiss one with a note |
| `GET /api/costs?month=` | Model spend in € per feature (decisions, Ask, briefings, shadows, replays) against the monthly cap |
| `GET /api/settings/history` | Every settings change with time, old and new value |
| `GET /api/export/{decisions,trades}.csv` | CSV export, outcomes included |

## Settings, secrets and persistence

Everything the agent knows survives a restart, and it always restarts paused.

- **Restart behaviour:** when the backend starts it loads state from SQLite, refreshes prices, re-checks stops and targets on open positions, and waits in Paused until Marc presses Resume. A pending decision from before the restart is marked expired.
- **Settings** live in the `settings` table as one validated JSON document with a schema version. Defaults are in code; unknown keys are dropped on load.
- **API key:** read from the OS keyring via `keyring`, falling back to `OPENAI_API_KEY` in a git-ignored `.env`. Every external source's key is handled the same way: stored per source in the keyring, with a .env fallback named after the source (for example COINGECKO\_API\_KEY). The frontend only ever sees whether a key is set and its last 4 characters. Keys are never logged and never appear in decision records, LLM briefs or exports.
- **Retention:** keep all decisions, trades and LLM calls. Raw briefs and responses older than 90 days may be pruned by a Settings button.
- **Export:** CSV of decisions and trades via the export endpoints.
- **Single engine:** the backend takes an exclusive lock file next to the database at startup and refuses to start if another instance holds it, so two agents never trade the same account.
- **Sleep and wake:** if the scheduler wakes more than one interval late, it skips missed cycles and runs one fresh cycle; it never replays them. Other scheduled jobs catch up once on start or wake: a missed morning briefing runs if it is within 4 hours of its scheduled time, otherwise it is skipped; a missed weekly review runs the same week; the eligibility check, outcome worker and backup run when their last run is older than their interval.

**Notifications**

| Event | Browser | Email |
| --- | --- | --- |
| Kill switch fired | on | on |
| Daily loss limit reached | on | on |
| Pair stale for 2 cycles in a row | on | off |
| Data source failing (3 failures in a row) | on | off |
| Model failing (3 calls in a row), daily budget reached, or monthly cost cap at 80% or 100% | on | on |
| Trade opened or closed | off | off |
| Shock brake on, or risk throttle on or lifted | on | off |
| Morning briefing and weekly review, at their configured times (replace the plain daily summary) | off | on |

- Browser notifications use the Notification API and work while the app is open in a tab; the app asks for permission on first use.
- Email goes over SMTP: host, port, TLS, username, sender and recipient in Settings; the SMTP password is stored in the keyring like every other secret.
- Each event can be switched per channel. The same alert is sent at most once per 30 minutes, with a count of repeats.
- "Send test" sends one test message to every enabled channel.

**Operations**

- Logs: structured JSON lines in `logs/app.log`, rotated at 10 MB, 5 files kept. Secrets are never logged.
- Errors: every error also lands in the activity log with kind "error"; the header shows an error badge with the count of the last 24h, linking to the filtered activity log. An error resolves itself when its cause has not recurred for 1 hour (for example a source that recovers); others are dismissed with a short note from the activity log. Errors that affected a decision or a fill are flagged and count for Readiness.
- Backups: a daily copy of the SQLite database at 03:00 using SQLite's backup API, into `backups/` inside the data folder, keeping the last 14.
- Health: `GET /api/health` reports engine state, last cycle, source status and recent errors, for later monitoring on the VPS.

## Testing and acceptance criteria

v0.1 is done when every item below passes on the simulated market, and the app runs for 24 hours on live Bitvavo prices without an unhandled error.

**Unit tests (pytest; Vitest for UI formatting helpers)**

- Indicators match reference values for RSI, EMA, ATR, VWAP and OBV on a fixed candle fixture.
- Risk gates: each gate passes and fails at its boundary. Sizing never exceeds risk %, exposure cap or cash.
- Model output: invalid JSON, wrong action for position state, stop above entry, and entry far from price end as HOLD with the post-check named, except R/R below the minimum, which ends as SKIP with the gate named.
- OpenAI client: timeout, 429 and 5xx retry once, then HOLD. Mock the SDK; no real calls in CI.
- Paper broker: fees and slippage applied on both sides; equity = cash + positions at last price.

**Engine tests (simulated market, fake clock)**

- 2,000 cycles run with no NaN anywhere in the snapshot. The total-open-risk cap sizes down a third concurrent full-size position (at 1% each, the third gets 0.5%; the test uses stops 4% away so cash is not the limit); a position open for 5 days closes by time stop; a stop-out blocks re-entry on that pair for 6 hours; Resume is refused while Killed until the kill is acknowledged; a cycle that finds no closed candle retries and then holds. A market below the eligibility bar cannot be activated, and a newly activated market shows Warming up until its history is loaded.
- A forced −8% drawdown flattens all positions and enters Killed within one ticker check; Resume is refused until the kill is acknowledged.
- A stop is hit between cycles and closed within 30 s of the price crossing it.
- Supervised mode executes after the veto window; veto prevents the fill; manual mode waits until the pair's next cycle, then expires; a pending trade expires early when price moves more than 0.5% from its planned entry.
- Restart mid-cycle resumes Paused with positions and history intact. The outcome worker labels a candle fixture correctly, counts stop first when one candle touches both levels, and backfills missed horizons after downtime.

**Acceptance checklist**

- [ ] Fresh install starts Paused, with Rules decider and no API key, and runs a cycle on Resume.
- [ ] With a key set, decisions show decider OpenAI, model id and prompt version.
- [ ] Every decision opens in the drawer with gates, reasoning, raw brief and raw response. The LLM inspector lists every call and its stored request matches byte-for-byte what was sent to OpenAI (verified against a mocked transport in tests). A fixture where the model calls RSI bullish while the brief has RSI 40 is flagged by the signal-claims check, and a brief with a NaN field is blocked by the input checks. For a given cycle, the Indicators screen opened at that decision shows the same values as the brief that was sent.
- [ ] Stop & flatten closes all positions after one confirm and pauses the agent.
- [ ] Live switch is visibly disabled and cannot be enabled; the Readiness screen shows every criterion with its value and status.
- [ ] Pulling the network cable marks pairs stale and produces HOLD, not errors or trades. Send test delivers a browser notification and an email; Rules decisions on a fixture brief match the Rules engine section exactly. Regime labels on a candle fixture match the definitions, including the 2-bar confirmation; a model BUY in a down regime ends as HOLD with the regime rule named. A third position correlated above 0.8 with two open ones is skipped; a 4% BTC move within an hour blocks entries for 2 hours but not exits; 3 losses in a row halve the next trade's risk; crossing an invalidation price triggers one model call for that pair; with the trailing stop on, a +2R trade's stop never moves down. Briefings and weekly reviews contain no number missing from their computed facts; Ask answers cite the records they used and refuse to trade or change settings; a What-if run with unchanged settings reproduces the actual results; a per-coin pause blocks entries but not exits. A WebSocket connection from another Origin is refused; two pending approvals that together exceed the open-risk cap fill only as far as the cap allows; a reset keeps decisions and outcomes; a market one day below the volume line stays eligible; reaching the cost cap switches decisions to Rules.
- [ ] No API key, for any source, ever appears in logs, the database, LLM briefs or exports; a source with a missing required key shows as disabled, not as errors.
- [ ] From a fresh clone, make run starts backend and UI on macOS and Linux with only uv and pnpm installed.

## Build order

Devin should ship this in eight PRs, each with its tests passing, so the agent is usable on paper before the OpenAI layer lands.

**Design handoff.** The repo has a `design/` folder: `DESIGN.md` (rules, component list, which PR builds which screens), `tokens.css` (colours, type, spacing), `screens/` (the 12 design screens as HTML, the source of exact styles and wording), `screenshots/` (one PNG per screen) and `CHANGES.md`. The spec wins on behaviour, the design on look and wording; differences go in the PR description. PR 1 builds every shared component on a hidden `/dev/components` page before any screen. Every PR that touches the UI attaches Playwright screenshots at 1440 and 1100 px wide next to the matching reference PNG.

1. **Skeleton:** FastAPI app with lifespan, SQLAlchemy + Alembic, Pydantic settings, lock file, local token, WebSocket stub. React + Vite app with generated API types; empty Dashboard and Settings render. Makefile with `dev`, `run`, `test`.
2. **Market data:** source registry with keyring-backed keys, Bitvavo client, simulated market, signals module with indicator fixtures and tests.
3. **Paper engine:** paper broker, risk gates and sizing, Rules decider, scheduler with pause, resume, run now, quarter-hour timing after candle close, 30 s exit watcher, total open risk cap, cooldown, time stop, and kill drawdown with acknowledgement. Engine tests pass on the simulated market.
4. **API and GUI:** the endpoints for this PR's screens and the snapshot WebSocket (each later PR adds the endpoints for its own features); Dashboard (KPIs, chart, latest decision, positions, activity), Decision drawer, Indicators screen, Performance screen, Settings, Stop & flatten and confirm dialogs, matching the design canvas.
5. **OpenAI decider:** brief builder, `prompts/decision.v1.md`, Structured Outputs call, post-checks, retries, Rules pre-filter, call budget, usage logging, keyring storage, Test key, and the LLM inspector.
6. **Execution modes and polish:** supervised and manual approval flow, outcome tracking and the outcome scorecard, notifications (browser and email), error badge, daily backups, CSV export, restart and sleep handling, README, acceptance checklist.
7. **Understanding and safety:** glossary and plain-language mode, "Why so quiet?" status, risk budget meter, per-coin pause, Readiness screen, daily briefing with the number check.
8. **Evidence:** Ask panel with read-only tools, Insights screen with calibration chart, What-if simulator, shadow strategies, weekly review and Reviews screen.

## v0.2: positioning data

v0.2 adds derivatives positioning data for BTC and ETH: what traders do with money in futures and options. It is built in three steps. Live decisions do not change until step 3 promotes a result, so the v0.1 measurement run stays clean.

### Step 1: collect (no effect on decisions)

An hourly collector stores six metrics for BTC and ETH from public, read-only endpoints. No API keys, no account or trading endpoints.

| Metric | What it shows | Source | Cadence | Backfill |
| --- | --- | --- | --- | --- |
| `funding_rate_settled` | Every settled funding rate, normalised to % per 8h | OKX v5, BTC-USDT-SWAP and ETH-USDT-SWAP | Each settlement | About 3 months |
| `funding_rate_current` | The current funding rate, % per 8h | OKX v5 | Hourly | None |
| `open_interest_usd` | Open interest in USD | OKX v5 | Hourly | As far as the API allows |
| `long_short_account_ratio` | Accounts net long ÷ accounts net short | OKX v5 trading statistics | Hourly | As far as the API allows |
| `dvol` | Deribit implied volatility index, hourly close | Deribit v2 | Hourly | Full |
| `skew_25d` | Implied vol of the 25-delta put minus the 25-delta call, in vol points, on the expiry closest to 30 days (expiries under 7 days ignored) | Deribit v2 option chain, computed | Hourly | None; history starts at collection |

- Storage: a `market_context` table with source, metric, symbol, ts (the data's own UTC timestamp), value, meta (JSON, for example the expiry used and the put, call and ATM IVs behind the skew) and fetched_at. Unique on source, metric, symbol and ts; writes are upserts. All rows are kept (about 15 an hour).
- Schedule: an hourly job at minute 5, separate from the 15-minute trading cycle. After sleep or a restart it runs once; settled funding and DVOL fill their own gaps from history on the next run.
- Isolation: a collector failure never delays, blocks or fails a trading cycle. Each request has a 10 s timeout and one retry on a network error, 5xx or 429; after that the failure is logged and the job moves on. Each API's rate limits are respected.
- Backfill: `uv run python -m backend.context.backfill --days 90` fills whatever history each endpoint provides and says which metrics could not be backfilled. Running it twice adds nothing.
- Visibility: Settings → Data sources and API keys gets rows for "OKX market data" and "Deribit options data" (key not needed), each with an on/off switch and the last successful fetch; off means no requests. `/api/health` shows each source's status and the age of its newest row. `GET /api/context?metric=&symbol=&from=&to=` returns stored rows for checking. No new screen.
- Helper for step 2: `percentile_90d(metric, symbol, at)` returns the value's percentile among that metric's values in the 90 days before it, or null with fewer than 30 days of data.
- Sources are confirmed reachable from the Netherlands before the build starts. Binance and Bybit no longer serve the Netherlands; OKX does. If OKX turns out to be unusable, the build stops and asks; it does not switch exchanges on its own.

### Prompt v2: decision quality (built before step 2)

The first 67 stored decision calls (all BTC-EUR, 21–22 October) showed that the model often cannot use the brief well. Every BUY was skipped by a gate (4 on costs vs risk with a 0.43% stop, 4 on R/R), and the same rejected BUY was proposed every 15 minutes for two hours because the brief never says what code did with it. Confidence was always 0.5 on HOLD and 0.8 on BUY. Invalidation was never filled in, so the early re-ask never fired. Signal names did not match the brief's keys, and one HOLD claimed stale data while every timeframe was fresh. `decision.v2` fixes how the model follows the existing rules. It does not change the strategy, the gates or the Rules pre-filter.

**Brief versions.** The prompt file's header declares `brief_version`. The brief builder adds the version 2 fields below only for prompts with `brief_version: 2` or higher, so calls with `decision.v1` keep exactly the v1 brief. A replay of a stored v1 brief with a v2 prompt first upgrades it: the v2 fields are computed from the stored brief, the decisions table and the stored candles at that time.

**New brief fields (version 2)**

| Field | Content |
| --- | --- |
| `previous_decision` | As in v1 plus: `at`, `outcome` (executed, skipped, held, vetoed, expired, unfilled or none), `failed_gates` (each with name, value and limit, for a skipped BUY) and `repeats` (how many decisions in a row ended the same way on near-identical levels) |
| `trade_math.stop_zone` | `highest`: the highest stop price that meets both the fee gate (`min_stop_pct`) and `min_stop_atr`; `lowest`: price − `max_stop_atr` × ATR(1h) |
| `trade_math.stop_candidates` | Up to 5 support levels whose stop price falls inside the stop zone, searched down to the zone's lowest price (deeper than the 3 levels in `structure`). Each: `id` (S1–S5), `level` (the support), `price` (level − `stop_buffer_atr` × ATR(1h); default buffer 0.1, a config value), `dist_pct` and `source` (1h swing, 4h swing or previous day low) |
| `trade_math.target_candidates` | Up to 5 resistance levels above price, including the previous day high when above price. Each: `id` (R1–R5), `price`, `dist_pct`, `source`. In a sideways regime only the nearest resistance is listed |
| `trade_math.pairs` | Every stop and target pair whose R/R, computed by code at the current price, is at least `min_rr`: `stop_id`, `target_id`, `rr`. Best R/R first, at most 10 |
| `trade_math.buy_feasible`, `infeasible_reason` | false when the regime is down (except BTC-EUR), when in a sideways regime price is more than 1 × ATR(1h) above the nearest support, when no support lies inside the stop zone, or when `pairs` is empty. The reason names which, with the numbers, e.g. "no support inside the stop zone (2.0% to 2.5% below price)" |
| `trade_math.conditions_1h` | The five 1h BUY conditions as true or false: `ema20_above_50`, `above_vwap`, `obv_bullish`, `rsi_bullish`, `adx_trending`; plus `conditions_1h_met` and `conditions_1h_required` (3, or 4 when the pair's regime is younger than 6 bars) |

`data_age_s` and `limits.max_age_s` stay in the brief for the inspector, but the v2 prompt has no staleness rule: stale data never reaches the model.

**Output schema (version 2)**: v1 plus two fields; signal names become an enum.

- `stop_basis`, `target_basis`: one of S1–S5 / R1–R5, or null. Required on BUY, null otherwise.
- `signals[].name`: one of `rsi`, `ema20_vs_50`, `vwap_dist`, `obv_trend`, `adx`, `atr_pct`, `atr_rank`, `vol_x_avg`, `ema200_dist`.
- On HOLD and SELL, `entry`, `stop`, `target`, `stop_basis` and `target_basis` are null.

**Post-checks added for v2 outputs**

- BUY while `trade_math.buy_feasible` is false → HOLD (failure).
- BUY whose `stop_basis` and `target_basis` are not a listed pair, or whose stop or target differs from that candidate's price by more than one price tick → HOLD (failure).
- BUY, SELL, or HOLD while holding, without `invalidation` or `invalidation_price` → warn.
- BUY with the same `stop_basis`, `target_basis` and failed gate as the previous skipped BUY on this pair, when `failed_gates` is unchanged → warn "repeated rejected BUY" (the gates skip it anyway).
- Levels set on HOLD or SELL → warn and ignore (as in v1).

**Validation before live uses it** (replays, not P&L: there are too few trades to compare results)

1. Replay `decision.v2` on every stored live decision call, each brief upgraded to version 2.
2. Compare v1 and v2 per call: invalid outputs, post-check failures, Signal-claims warnings, invalidation present on BUY, repeated rejected BUYs, BUYs that would pass every gate, agreement with the Rules baseline, the spread of confidence values, tokens and cost.
3. Switch live to `decision.v2` (Settings → Decision model → Prompt version, logged in the settings history) when v2 has no more invalid outputs than v1, Signal-claims warnings on under 10% of calls, invalidation on at least 95% of BUYs, no repeated rejected BUYs and no regime violations. Otherwise revise as `decision.v2.1` and replay again.
4. The switch is logged and starts a new comparison window; Performance, Insights and the scorecard already group results by prompt version.

The sentiment block of step 2 moves to `decision.v3` (v2 plus the sentiment block), so Shadow A still differs from live in one thing only.

### Step 2: sentiment block, tested as a shadow

Starts when every metric has at least 30 days of data and the v0.1 measurement run has lasted at least 4 weeks.

- Brief: a new `sentiment` block with, for BTC and ETH, the current funding rate, open interest and its 24h change in %, the long/short ratio, DVOL and the 25-delta skew. Every value comes with its 90-day percentile and a read: extreme high at the 90th percentile or above, extreme low at the 10th or below, otherwise normal. Briefs for other coins carry the BTC and ETH values labelled market-wide. Freshness limit: 2 hours (9 hours for settled funding). A stale or missing value is sent as null with the reason, and never blocks a decision.
- Prompt: a new file, `decision.v3.md` (`decision.v2` plus the sentiment block, `brief_version: 3`), explains the block. Positioning extremes are contrarian warnings: they can lower confidence or turn a BUY into a HOLD, and are never a reason to buy on their own. The output schema stays the same as v2. The signal-claims check also covers the sentiment reads.
- Test: Shadow A runs the live model with `decision.v3` against live on `decision.v2`. The sentiment block is the only difference, so the comparison measures what it adds.
- No gates use sentiment data in v0.2.
- GUI: the Decision drawer shows the sentiment block (value, 90-day percentile and read per data point) below the signal stack, and two new rows in its safety gates: News flag and Macro event. Insights gets a positioning slice: BUY outcomes grouped by the funding and skew reads at entry.

### Step 3: decide

After Shadow A has at least 30 closed trades, compare it with live over the same window, leaving out any shadow pause windows: average R after fees, max drawdown, profit factor, and missed vs avoided for HOLD and SKIP. Promote `decision.v3` when its average R is at least 0.05R higher and its max drawdown is no worse; otherwise keep `decision.v2`, keep collecting and look again after 30 more trades. Either way the result is logged in the decisions below.

### Risk flags

- **News flag:** a switch in Settings and on the Dashboard that blocks new entries on all pairs until cleared, for events such as an exchange hack or a major regulatory decision. Exits keep working; it appears in the quiet line and is logged. Ships with step 2 and applies to live and shadows.
- **Macro calendar:** `config/macro_events.yaml` lists FOMC decisions, US CPI releases and US jobs reports (date, time UTC, type), kept up to date by Marc. A "Macro event" gate blocks new entries from 6 hours before to 1 hour after an event. Because the dates are known in advance, it is tested in What-if over the v0.1 period first, and switched on only if it does not lower average R.

### Limit entries, tested as Shadow B

v0.1 fills every order at market: the taker fee (0.25% per side) plus 0.05% slippage, so the fee gate needs stops at least 2% away and targets at least 4%. Limit entries pay the lower maker fee but do not always fill, and the trades that leave without you are often the best ones. This feature adds limit entries with an honest paper fill model and measures both effects as a shadow before live uses it.

- **Setting:** Entry orders: Market (default) or Limit (post-only). Settings get a maker fee per side (default 0.15%; check Bitvavo's current fee schedule for the account's tier) next to the existing fee, which is renamed taker fee.
- **Order types per leg:** with Limit selected, entries and targets are limit orders at the maker fee without slippage. Stops, time stops, the kill switch, Stop & flatten and manual closes always fill at market: taker fee plus slippage.
- **Fee gate:** costs are the entry fee plus the costs of exiting at the stop, because risk is measured to the stop. Market entries: 2 × 0.25% + 2 × 0.05% = 0.6%, so a stop at least 2% away. Limit entries: 0.15% + 0.25% + 0.05% = 0.45%, so a stop at least 1.5% away and a 2.0R target at least 3% away. `limits.min_stop_pct` in the brief follows the order type.
- **Placing a limit entry:** the limit price is the decision's entry price. Post-only cannot cross the spread: if the entry is at or above the best ask when the order is placed, the limit moves to the best bid. Size, R/R and every gate are computed at the limit price when the order is placed and checked again at fill.
- **Fill rule:** the 30-second watcher fills the order only when the last traded price is at least one price tick below the limit. A touch is not a fill, because queue position is not modelled; this is the conservative proxy. Fill price = the limit price.
- **Expiry:** an unfilled entry is cancelled at the pair's next cycle (15 minutes), or at once when a gate starts blocking entries (shock brake, news flag, entry pause, daily loss limit, kill switch). The decision gets the new status `unfilled`, and its outcome is tracked like a BUY that was not taken (from the decision's entry, stop and target), so missed winners and avoided losers are counted.
- **Targets:** with Limit selected, the target rests as a limit order and fills by the same through-the-price rule. The stop is checked by the watcher as today.
- **Data:** a `paper_orders` table (decision_id, account_id, pair, side, leg, limit_price, qty, placed_at, expires_at, status, filled_at, fill_price). `Decision.status` gains `unfilled`.
- **What-if** keeps market orders: stored 1h candles and exit ticks cannot tell whether a limit would have filled within 15 minutes. Limit entries are measured only as a shadow.
- **Shadow B:** the live model and prompt version with the override Entry orders = Limit. Prompt and model match live, so the order type is the only difference. When live switches prompt version, Shadow B follows and its comparison window restarts, so it is best started once live runs `decision.v2`. It does not wait for step 2.
- **GUI:** Settings → Market data & paper account: Entry orders (Market / Limit) and the maker fee. The shadow row shows fills and missed fills. The Decision drawer shows a limit entry's state: placed, filled or unfilled, with times and prices. Compare for Shadow B adds fill rate and the average R that missed fills would have made.
- **Decide:** after Shadow B has at least 30 closed trades, compare it with live over the same window, as in step 3. Switch live to limit entries only when Shadow B's average R after fees is at least 0.05R higher and its max drawdown is no worse. If missed fills would have averaged clearly more R than filled ones, that is adverse selection: keep market entries.

### Build order (v0.2)

9. **Market context collector:** step 1.
10. **Sentiment block:** the `sentiment` brief block (`brief_version: 3`), `decision.v3.md`, signal-claims check, Decision drawer and Insights additions, the news flag. Built after PR 13.
11. **Macro calendar:** the events file, the gate (off by default) and its What-if support.
12. **Limit entries:** the Entry orders setting, maker fee, paper fill model, `paper_orders` table, `unfilled` status and outcomes, the fee-gate change, Shadow B support and the GUI parts above.
13. **Decision quality (prompt v2):** brief versions, the version 2 brief fields (`previous_decision` outcome, `trade_math`), the version 2 output schema, the v2 post-checks, upgrading stored v1 briefs for replays, and a replay comparison report. `decision.v2.md` is written by Marc; build this before PR 10.

### Not in v0.2

Fear & Greed, BTC dominance and market cap trend (the next group, once v0.2 has a result), on-chain flows, news tone scores, social media sentiment, traditional markets, and hosting on a VPS, which gets its own section when the v0.1 measurement run ends.

## After v0.1: sentiment and external market context

Once v0.1 runs and outcomes are being recorded, add these sources one at a time and keep each only if replays and outcomes show it improves decisions.

**Sentiment and positioning** (a new `sentiment` block in the brief)

Positioning data (what traders do with money) is preferred over opinion data (what people say). Every value is sent with its percentile over the last 90 days, so the model sees extremes, which is where sentiment carries information, usually as a contrarian warning.

| Data point | What it shows | Source | Freshness limit |
| --- | --- | --- | --- |
| Funding rate, BTC and ETH perpetuals | Cost of holding leveraged longs; very positive means crowded longs | Public futures APIs (Binance, Bybit or OKX); confirm which are reachable from NL | 1 hour |
| Open interest and its 24h change | Whether new leverage is entering; rising OI with rising price confirms a trend, rising OI with flat price builds squeeze risk | Same futures APIs | 1 hour |
| Long/short account ratio | How one-sided traders are; extremes tend to precede squeezes | Same futures APIs | 1 hour |
| Options implied volatility (DVOL) and 25-delta put/call skew, BTC and ETH | How much traders pay for protection; a fear gauge backed by money | Deribit public API | 1 hour |
| Fear & Greed Index | Overall crowd mood, used as a slow regime signal | alternative.me | 26 hours |

Briefs for every other coin use the BTC and ETH values as market-wide sentiment, labelled as such.

**Market structure and risk flags**

| Data point | What it shows | Source | Freshness limit |
| --- | --- | --- | --- |
| BTC dominance and total crypto market cap trend | Whether the market grows or money rotates between coins | CoinGecko global endpoint | 2 hours |
| Scheduled macro events (FOMC, US CPI, US jobs report) | Flag for an event within 6h, so new entries can wait | Dates file in config | n/a |
| Hard news events (exchange hack, ETF decision, major regulation) | Flag only, never a sentiment score; a flag blocks new entries until cleared | Manual flag in Settings first; an automated source later | Until cleared |

Funding, open interest, the long/short ratio and options data are the first group and are specified in v0.2 above. Add the other groups one at a time, and keep each only if replays and outcomes show it improves decisions. Not planned: social media sentiment (expensive, noisy, easy to manipulate) and news tone scores. Later, only if outcome data shows value: traditional markets (S&P 500 or Nasdaq futures, dollar index), liquidation data, and on-chain flows.

**Later features** (after going live, or once running on the VPS)

- **Phone app (PWA) with push notifications**, installable from the VPS-hosted UI.
- **Custom alerts:** user-defined conditions such as "a BTC setup appears" or "ETH crosses €2,500", delivered through the existing notification channels.
- **Dutch tax export:** the value of holdings on 1 January (the box 3 reference date) and a yearly overview of trades, fees and realised results as CSV and PDF.

## Decisions

- [x] Default model: gpt-4o-mini during development.
- [x] Trade the 1h timeframe with 4h and daily context; cycle every 15 minutes (up to 96 calls per active pair per day before pre-filtering, about 860 for the default nine); fee gate at 30% of risk.
- [x] The model is called only when Rules sees a setup or a position is open.
- [x] Paper balance €10,000; fees at Bitvavo's entry tier, 0.25% taker per side.
- [x] Default execution mode: Automatic.
- [x] Hosting: develop and test locally; production runs on a VPS later (provider to be chosen), which adds login, HTTPS and a deployment setup.
- [x] Portfolio risk: total open risk across all positions at most 2.5% of equity.
- [x] Kill switch recovery: acknowledge in a dialog, which resets peak equity, then Resume.
- [x] Alerts: browser notifications and email.
- [x] Exits: time stop after 5 days, and a 6-hour cooldown after a stop-out. No partial take-profit in v0.1.
- [x] Markets: any Bitvavo EUR market that passes the eligibility check; default active set BTC, ETH, SOL, XRP, ADA, LINK, AVAX, DOGE, LTC.
- [x] User features 1–11 (Ask, quiet status, plain-language mode, daily briefing, readiness checklist, per-coin pause, risk meter, What-if, Insights, shadow strategies, weekly review) are in v0.1; phone app, custom alerts and Dutch tax export come later.
- [x] Model costs: a monthly € cap (default €10) over every call plus per-feature limits; trading decisions keep priority; costs shown in euros.
- [x] What-if: stored real fills make an unchanged run match reality; missing model calls are answered by Rules and marked as stand-ins.
- [x] Reset: "Reset paper account" keeps the learning data; "Delete all data" is a separate, typed confirmation.
- [x] Supervised mode: each pending trade has its own card and timer; gates are re-checked at fill.
- [x] Readiness: calibration is the weighted gap over confidence buckets; errors auto-clear after 1 hour or are dismissed with a note.
- [x] Market eligibility: fails only after 3 days below a threshold or at once 20% beyond it; 3 days to pass again.
- [x] Ask is a side panel over any screen; the glossary lives in tooltips and Ask, not a separate page.
- [x] Insights: 30 decisions for a finding, 10–29 shown as early signal.
- [x] Regime: a trend × volatility label computed in code, sent in the brief, stored with every decision, with prompt rules per regime enforced by a code check; one ADX threshold (25) everywhere.
- [x] Portfolio safety in code: at most 2 positions correlated above 0.8, a 2-hour shock brake on sudden moves, and risk halved after 3 losses or a 4% drawdown.
- [x] The model returns an invalidation price; crossing it triggers an immediate re-evaluation, not an exit. Trailing stop from +2R exists as a setting, off until tested. Model calls use temperature 0.
- [x] Not yet: sizing by confidence (until calibration passes) and feeding the agent its own track record (feedback loops on small samples).
- [x] Stops may sit 0.5–5 × ATR(1h) away; when no stop in that range is wide enough for the fee gate, the BUY is skipped.
- [x] Exposure cap 100% (spot, no leverage): cash and the 2.5% open-risk cap are the real limits.
- [x] Cycles are fixed at 15 minutes in v0.1.
- [x] A pending approval expires at the pair's next cycle or after a 0.5% move; R/R, size and gates are recomputed at the fill price.
- [x] Strategy research (October 2026, see docs/RESEARCH.md): after costs, the Rules BUY on 1h or 4h, range trading and a regime allocator did not beat a static BTC + cash mix at the same drawdown. New strategy features are paused: PR 10 and 11 wait; PR 12 is finished only if nearly done. PR 13 (prompt v2) goes ahead (decided 24 October 2026) because it improves decision quality and measurement without changing the strategy. The paper agent, the v0.2 collector and the fix PR continue.
- [x] Rule for strategy changes: an idea goes into this spec only when, in the research script (`tools/regime_history.py`), it beats the static BTC + cash mix at the same maximum drawdown in the training and the test period, and the sensitivity grid around its settings agrees.
