# Design changes

Newest first. Each entry names the screens it touches.

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
