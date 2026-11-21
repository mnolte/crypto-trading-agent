---
item: 026
type: feat
title: Universe screen
status: ready
issue: 46
kickoff: 026-feat-universe-screen.md
spec_prs: [49]
build_prs: []
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
