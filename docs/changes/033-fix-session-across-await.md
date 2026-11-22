---
item: 033
type: fix
title: DB pool exhaustion — sessions held across network awaits
status: merged
issue: 66
kickoff: —
spec_prs: []
build_prs: [PR]
---

# 033 · fix · DB pool exhaustion — sessions held across network awaits

## 2026-11-23 · build PR #PR

On 2026-11-22 the API's SQLAlchemy QueuePool (size 5 + overflow 10)
exhausted during a Bitvavo outage: responses took ~78 s, and every code
path that kept a Session — and its checked-out pool connection — across
a network await pinned one of the 15 connections for the whole fetch.

The fix ends the transaction before every network await instead of
holding it:

- `GET /api/universe` fetches prices before opening its session, and a
  new `AppState.universe_prices_lock` makes concurrent polls share one
  in-flight `ticker_prices` fetch.
- `run_eligibility_check` rolls back after each market's check, and
  `check_market` rolls back before the full-history fetch, so no
  connection spans the per-market candle requests.
- `GET /api/indicators/{pair}` rolls back before the order-book and
  ticker fetches inside `_liquidity`.
- `activate_market` rolls back before the on-demand check for a market
  that was never screened.
- `_retry_candles` rolls back after a failed upsert, and
  `refresh_market_data` commits its activity rows before awaiting the
  notification sends.

Tests: `test_check_never_holds_transaction_across_fetches` proves every
`candles()` call during the eligibility check sees a closed transaction,
and `test_prices_fetch_shared_between_concurrent_callers` proves two
concurrent price refreshes share one fetch.
