# Design changes

Newest first. Each entry names the screens it touches.

## 2026-10-22 — Prompt versions in captions

- The sentiment prompt is now decision.v3 (spec: "Prompt v2: decision quality").
  Decision drawer positioning caption, States stale-source note and the Shadow A row
  say decision.v3; the Shadow B row uses v2. Wording only.

## 2026-10-21 — v0.2: limit entries (Shadow B)

- Settings → Market data & paper account: fields in a 2 × 2 grid (starting balance,
  slippage on market orders, taker fee, maker fee); new "Entry orders" segmented
  control (Market / Limit post-only) with a note on minimum stops and Shadow B.
- Settings → Shadow strategies: Shadow B row (decision v1, limit entries, "7/10
  filled", tooltip with the missed-fill average); "Add shadow" disabled at 2 of 2;
  Promote text mentions the entry order type.
- States: Decision drawer footer for a limit entry in three states: waiting for
  fill, filled (with fee and size), unfilled (cancelled, tracked as a missed fill).

## 2026-10-14 — v0.2: positioning data and event flags

All additions carry a blue v0.2 tag in the screens (handoff only, not shown in the app).

- Decision drawer: "Market positioning · BTC" section below the signal stack (funding,
  open interest, long/short accounts, DVOL, 25-delta skew; value, 90-day percentile,
  read with extremes in amber); safety gates gain News flag and Macro event rows.
- Dashboard: News flag button between Pause and Stop & flatten; when on, an amber banner
  with Clear flag, and the "This cycle" line adds "News flag on since 04:20." before
  per-coin pauses.
- Settings: Data sources table now has "OKX market data" and "Deribit options data" (on,
  with days stored); CoinGecko and alternative.me read "Off · after v0.2". Risk limits
  gain an "Event flags" block: news flag and macro event gate (off by default, with a
  link to What-if).
- States: news flag banner, macro event banner, "Set the news flag?" dialog with an
  optional reason, and a stale-source row.
- Insights: a "Positioning · v0.2" slice tab (BUY outcomes by funding and skew read at
  entry).
- What-if: macro event gate option in the scenario.

## 2026-10-11 — ADX wording

- Indicators: ADX pane label and the reads footnote say "25 or more = trending"
  (inclusive, ≥ 25), matching the spec.

## 2026-10-10 — Spec review fixes

- Settings: the cycle-interval selector is gone (cycles are fixed at 15 minutes in v0.1);
  max total exposure is 100%; max stop distance is 5 × ATR(1h); the eligibility note
  asks for 300+ days of daily history.
- Dashboard: exposure KPI shows "cap 100% · spot, no leverage"; sample BTC trade now
  uses the full 1% risk (0.0807 BTC), so exposure, open risk, positions and activity
  changed with it; SOL's stop sits at breakeven (139.10) as the briefing says.
- Decision drawer and States: same BTC numbers; exposure gate reads "≤ 100%".

## 2026-10-10 — First handoff

All 12 screens: Dashboard, Decision detail, Performance, Settings, States and dialogs,
Indicators, LLM inspector, Insights, What-if, Ask the agent, Reviews, Readiness.

Includes the regime chips and band, the portfolio safety rules (correlated positions,
shock brake, risk throttle), the invalidation price in the decision drawer, the monthly
cost cap, the approval queue and the Ask side panel.
