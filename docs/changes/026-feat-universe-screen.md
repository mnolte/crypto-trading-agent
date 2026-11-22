---
item: 026
type: feat
title: Universe screen
status: building
issue: 46
kickoff: 026-feat-universe-screen.md
spec_prs: [49]
build_prs: [58]
---

# 026 · feat · Universe screen

## 2026-11-21 · spec PR

Marc, 21 Nov: "I want a universe page, just like in the stock trading agent. This page should provide an overview of the symbols available and their status." New `docs/SPEC.md` → v0.3 → Universe screen, replacing the Markets screen of build plan item 017, which is dropped (no kickoff was written for it). How markets become eligible, active or failing doesn't change.

- **A Universe screen in Control,** after Readiness, as in Stock Trading Agent (its item 077): the check line, six tiles, Data sync, Today's check as a funnel, and a table of every Bitvavo EUR market with one status each, filters, search, Activate, Deactivate and the entries pause.
- **One status per market** with a fixed order for active markets: warming up, failing, 1 or 2 of 3 fails, entries paused, held, active; and for the rest: eligible, recovering, excluded, held but not active.
- **Stored check runs and sync jobs:** new tables `market_checks` (90 days) and `sync_runs` (30 days), so the screen can show the funnel and what loaded when. A failed backfill retries with the next cycle.
- **Bad data keeps the previous result:** when Bitvavo's lists fail, or volume or spread is missing for `market.check_data_error_pct` (50) percent or more of the previously eligible markets, no market's streak moves and the check line says which result is in use. New setting, in Settings → Markets → Advanced.
- **New columns:** name (from Bitvavo's `/assets`), the last price, 30-day return against BTC and correlation with BTC (information only), eligible and failing since. Volume and spread become Decimals (hard rule 2).
- **API:** `GET /api/universe`, `/api/universe/check`, `/api/universe/sync`, `POST /api/universe/sync/{job}/retry`. `GET /api/markets` stays for the Dashboard; activation and deactivation keep `PUT /api/markets/{market}`, now with item 025's looser confirmation and reason.
- **Settings → Markets** keeps the thresholds and links to the screen; the market table leaves Settings. The Needs you item for a failing market opens the screen on the Failing filter; the Dashboard's Markets card title links to it.
- Also changed: Supported markets (a pointer), the v0.1 GUI Settings note, the per-coin pause (set from the Universe table), the SQLite tables, the API table, Build order (v0.3) (PR 17 replaced; item 026 after item 025), Decisions. `docs/README.md` maps 17 to this item; the register row of 017 says dropped.

## 2026-11-22 · build PR (backend)

Backend half of the item; the screen follows in a second PR.

- New setting `market.check_data_error_pct` (default 50, range 1–100, unit %, neutral direction) in the markets section; metadata in `settings_spec.py` and `settings_directions.py`.
- Migration `0016`: `markets` gains `name`, `last_price`/`price_at`, `vs_btc_30d_pct`, `corr_btc_30d`, `eligible_since`, `failing_since`; `volume_30d_eur` and `spread_pct` are now Decimals stored as text (the float columns are converted through `Decimal(str(v))`, volume to 0.01 and spread to 0.0001, half to even). New tables `market_checks` (90 days) and `sync_runs` (30 days).
- `MarketClient.assets()` on the protocol, Bitvavo (`GET /assets`) and the simulated client; `ticker_prices()` gives every market's last price in one `GET /ticker/price`.
- `run_eligibility_check` stores a `market_checks` row per run (trigger startup/daily/retry, thresholds, funnel, kept/held-back). A data error — `/markets` or `/ticker/24h` failing, or volume/spread missing for `check_data_error_pct`% or more of previously eligible markets — keeps every streak and stores a failed run with `eligibility.check_failed` at WARNING.
- Funnel order per spec: EUR markets → not stablecoin → trading → volume → spread → daily candles → pass every rule. vs-BTC return and BTC correlation come from the last 31 closed daily candles (30 returns); BTC-EUR gets nulls.
- `sync.py`: `SyncRuns`/`SyncRun` (throttled progress, per-market/source failures with `next_retry`, 30-day retention). Jobs: `exchange_rules` and `daily_check` inside `refresh_market_data`, `candles` once per cycle in the engine, `backfill:<market>` one run per market.
- `PUT /api/markets/{market}` accepts `confirm_looser` (with `confirm` kept as the item-025 alias) and returns before the backfill finishes — the backfill is a supervised task; a second activation while one runs is a no-op. A 409 `looser_needs_confirm` now also reverts the market row.
- `GET /api/universe` (check line, tiles, calls/day, every market with one status and its line), `GET /api/universe/check`, `GET /api/universe/sync` (latest run per job, backfill progress with pace-based ETA, watcher state), `POST /api/universe/sync/{job}/retry` (404 unknown job, 409 during a cycle or when the job did not fail). Failed backfills retry their failed timeframes at the next cycle.
- Prices refresh at most once a minute; a failed fetch keeps the cached prices. The Needs-you failing-market item opens `/universe?filter=failing`.
- Status precedence per spec: active → warming up, failing, 1–2 of 3 fails, entries paused, held, active; not active → held-not-active, eligible, recovering, excluded.

Tests: `backend/tests/test_universe_screen.py` (funnel, bad-data retention at and below the threshold, vs-BTC/correlation, every status kind, sync lifecycle and retry-due, retention, activation returning early, Decimal round-trip, the four endpoints). `test_needs.py` updated for the new link target.
