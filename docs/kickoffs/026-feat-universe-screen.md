# 026 · feat · Universe screen

Kickoff for Devin. Start the session with: "Build docs/kickoffs/026-feat-universe-screen.md."

A Universe screen as in Stock Trading Agent: every Bitvavo EUR market with its status, the daily check as a funnel, what the app loads and when, and Activate, Deactivate and the entries pause per market. The market table leaves Settings, which keeps the thresholds. How markets become eligible, active or failing doesn't change, except that a check with bad data now keeps the previous result. This item replaces build plan item 017 (Markets screen). Spec: `docs/SPEC.md` → v0.3 → Universe screen (new), Market data and paper broker → Supported markets, Settings screen → Markets, the API table. Design: `design/screens/Universe.dc.html` with `Universe.png` and `Universe-390.png`; the Universe item in every sidebar; `Settings.dc.html` → Markets; `design/DESIGN.md` → Universe screen. Issue #46.

```text
026 (Universe screen). Goal: a Universe screen that shows every Bitvavo EUR
market, its status and the daily check, as v0.3 → Universe screen
describes, with Activate, Deactivate and the entries pause per market.
Decisions, gates, sizing, exits and the eligibility rules don't change.
Start from the latest main, after both PRs of item 025 are merged: this
item uses its settings versions, reasons and looser confirmation, and
changes its Settings → Markets section.

Names (docs/work-items.md): branch feat/026-universe-screen, PR titles
"[026] feat: Universe screen (backend)" and "[026] feat: Universe screen
(screen)", commits start with [026], screenshots in docs/screenshots/026/.
Update docs/changes/026-feat-universe-screen.md in each PR: add the PR to
build_prs, status building in the first and merged in the last, and a dated
section "## YYYY-MM-DD · build PR #n" saying what changed in behaviour.

Read first:
1. docs/changes/026-feat-universe-screen.md and
   design/changes/026-feat-universe-screen.md
2. docs/SPEC.md → v0.3 → Universe screen (all of it); Market data and paper
   broker → Supported markets; v0.3 → Settings screen (Markets, Saved
   straight away, Tighter and looser, API); v0.3 → Shell and Dashboard
   (Needs you, Markets card); the SQLite tables and the API table
3. design/DESIGN.md → Universe screen and Which PR builds which screens;
   design/screens/Universe.dc.html and both screenshots (texts are final);
   Settings.dc.html#markets; States.dc.html (card states, toasts)
4. AGENTS.md → hard rules 2 (money as Decimal; the frontend does no money
   arithmetic), 3 (thresholds from config), 4 (a failure never trades),
   10 (no network in tests), 11 (UTC in logic, Amsterdam only on screen)
5. Stock Trading Agent's Universe screen and its sync_runs code, for
   reference only

PRs: two. The first (backend) says "Part of #46" and moves this kickoff
unchanged to docs/kickoffs/archive/ with git mv. The second (screen) says
"Closes #46" after you have checked every point of the issue against main
plus the PR and listed them under "Issues closed".

What exists today (main on 21 November):
- backend/app/agent/market/universe.py: failed_rules, beyond_threshold,
  apply_check (the 3-day streaks), check_market, run_eligibility_check,
  warming_up, entries_allowed, backfill_market, activate_market (awaits the
  backfill inside the request), deactivate_market.
- MarketRow (db.py:63) stores volume_30d_eur and spread_pct as floats.
- main.py: refresh_market_data runs at startup and from daily_market_loop at
  01:00 UTC; a failure only marks the bitvavo source.
- api/sources.py: GET /api/markets (list, used by the Dashboard Markets card
  and Settings) and PUT /api/markets/{market}; api/understanding.py: the
  pause endpoints. logevents.py already has eligibility.failing and
  eligibility.check_failed.
- frontend/src/pages/Settings.tsx (after 025): Markets keeps today's table
  under the thresholds, with "Open Markets" shown as unavailable.

Steps, PR 1 (backend), one commit each:

1. Setting. market.check_data_error_pct: Decimal = 50, range 1–100, in
   MarketSettings, backend/config.example.yaml, the 025 ranges, and the
   025 directions map as neutral ("how much missing data counts as a data
   error, not a rule"); section markets in /api/settings/meta.
2. Migration (a new file in backend/alembic/versions, never an edit):
   - markets: add name, last_price and price_at, vs_btc_30d_pct,
     corr_btc_30d, eligible_since, failing_since; volume_30d_eur and
     spread_pct become Decimal stored as text (copy existing values with
     Decimal(str(v)), volume to 0.01, spread to 0.0001, half to even).
   - market_checks: id, started_at, finished_at, trigger (startup, daily,
     retry), status (ok, failed), error, thresholds JSON, funnel JSON (per
     step: rule, left, dropped markets), kept JSON, held_back JSON.
   - sync_runs: id, job, started_at, finished_at, status (running, ok,
     partial, failed), done, total, failures JSON (market or source, error,
     next_retry), updated_at.
3. Names. MarketClient.assets() -> {symbol: name} from Bitvavo's public
   GET /assets, in bitvavo.py and sim.py (sim: "Bitcoin", "Ethereum" …).
   The exchange-rules run fills markets.name; a missing name stays null and
   the screen shows "name pending".
4. The check (universe.py), in this order, all values Decimal:
   - fetch 31 closed daily candles per market (one more than today) so 30
     returns exist; volume stays the average of the last 30 days;
   - compute every market's metrics first, then decide:
     a data error is /markets or /ticker/24h failing, or volume or spread
     missing for check_data_error_pct percent or more of the markets that
     were eligible before the check. Then nothing in markets changes, the
     market_checks row is failed with the error in plain words, and
     eligibility.check_failed is logged once at WARNING;
   - otherwise apply_check as today, and set eligible_since when a market
     becomes eligible and failing_since when an eligible one loses it
     (cleared on recovery);
   - vs_btc_30d_pct = ((close_t / close_t-30) − (BTC close_t / BTC
     close_t-30)) × 100, to 0.1; corr_btc_30d = Pearson correlation of the
     30 daily simple returns with BTC-EUR's, to 0.01; null for BTC-EUR and
     when 31 closes aren't there;
   - store the funnel in the order of the spec: EUR markets, not a
     stablecoin or fiat token, trading, volume, spread, 300 daily candles
     (counted only for markets that passed volume and spread), pass every
     rule today; kept = eligible markets that failed today; held_back =
     markets that pass today but aren't eligible yet.
   Thresholds keep coming from MarketSettings (hard rule 3).
5. Sync jobs: backend/app/agent/market/sync.py with SyncRun (start(job,
   total), progress(done), fail(target, error, next_retry), finish()), on
   the injected clock, writing its row at most once a second. plain_error(
   exc) turns errors into words: 429 "Bitvavo: too many requests (429)", a
   timeout "Bitvavo didn't answer within 10 s", other HTTP "Bitvavo: HTTP
   503", anything else the exception's class name. Wrap:
   - exchange_rules: the rules and names part of refresh_market_data;
   - daily_check: run_eligibility_check (total = EUR markets);
   - candles: the cycle's candle fetch for active markets (engine.py
     _fetch_closed and _load_context), one run per cycle;
   - backfill: one run per market (total = 800 + 300 + 300 by default, from
     config), done = candles stored; a timeframe that fails is recorded with
     next_retry = the next cycle's start, or Bitvavo's reset time when
     that is later. Before each cycle, backfills whose next_retry has
     passed run again for their failed timeframes.
   The daily job deletes sync_runs older than 30 days and market_checks
   older than 90 days (only those rows). Prices for stops isn't a job: the
   screen reads the exit watcher's last check and error.
6. Activation without waiting. activate_market saves the row and starts the
   backfill with spawn() (PR 15) instead of awaiting it, so PUT returns at
   once and the screen shows the progress. A second activation while one
   backfill runs for that market is a no-op.
7. Status: one function, universe.market_status(row, position, pause,
   warming, cfg) -> MarketStatus(kind, facts), used by GET /api/universe,
   the Needs you item and the Dashboard card (never computed twice). Kinds
   for an active market, the first that applies: warming_up, failing
   (active and not eligible), fails (eligible, below_streak 1 or 2),
   paused, held, active. Otherwise: eligible, recovering (not eligible,
   within_streak ≥ 1), excluded, held_inactive. facts carry what the line
   needs (failed rule, value and limit, streak, days, since, quantity,
   pause until, candles per timeframe, last decision and its time, the
   Rules condition); the frontend writes the sentence.
8. API (regenerate openapi.json and the frontend types):
   - GET /api/universe: check line (last check time, status, error, the
     time of the result in use), tiles, calls {per_market_per_day: 1440 /
     cycle minutes, active_per_day, daily_call_budget}, and every market
     with name, status, facts, last_price and price_at, volume, spread,
     vs_btc, corr, regime (active only), activated_at. Prices come from
     one GET /ticker/price for all markets, cached 60 s on the app state;
     a failed fetch keeps the cached prices and their time.
   - GET /api/universe/check: the latest market_checks row, and the one in
     use when the latest failed.
   - GET /api/universe/sync: the latest run per job, the watcher's state,
     and while a backfill runs, candles loaded of needed and the seconds
     left from its own pace (null in its first 5 s).
   - POST /api/universe/sync/{job}/retry: reruns a failed job for its failed
     markets; 409 during a cycle or when the job's latest run didn't fail;
     404 for an unknown job.
   - PUT /api/markets/{market}: as 025 left it (reason "Activated X" or
     "Deactivated X", source action, 409 looser_needs_confirm for an
     activation without confirm_looser).
   - Needs you: the failing-market item links to /universe?filter=failing.

Steps, PR 2 (screen):

9. Route /universe; the Universe item in the sidebar's Control group after
   Readiness and before Logs, with the grid icon from the design, also in
   the phone menu. Page title and sub line as in the design.
10. The page per Universe.dc.html, top to bottom: the check line (amber when
    the last check failed, with its error and the result in use; "Check
    settings" opens Settings → Markets), the tiles (zero in the normal
    colour), Data sync (one line when fine; opens itself when a job failed,
    is late or runs, the header saying why; the progress bar while a
    backfill runs; Retry), Today's check (bars, dropped markets, the
    eligible line, "How the check works" closed), and the table.
11. The table: chips and lines per status kind as in the design; filters
    All, Active, Held, Eligible, Failing, Warming up, Excluded with counts,
    kept in ?filter=; search on pair and name; any column sorts; the
    default order and the fold rows as the spec says (All: up to 6 active,
    never hiding one that needs a look; 2 eligible; every recovering one; 3
    excluded). Activate opens the in-row confirm with the looser tag and
    the calls before and after against the budget, then PUT with
    confirm_looser true; Deactivate opens its in-row confirm; the clock
    opens the existing pause choices; Resume when paused. Errors from PUT
    show in the row. Refetch /api/universe/sync every 2 s while a job runs,
    and /api/universe every 60 s while the page is open and on every
    snapshot message.
12. Settings → Markets: remove the market table and its search; one row
    "Active markets" with the count, the names and "Open Universe"; the
    Advanced fold gets "Data error share". The Dashboard's Markets card
    title links to /universe.

Tests (each rule a pass and a fail case; no network, each test its own
database, injected time; proven in CI, never run the full suites locally):
- check: the design's funnel on a fixture of 236 markets gives 229, 226,
  20, 16, 15, 15 and eligible 15 with AVAX kept and SHIB held back; history
  is fetched only for markets passing volume and spread;
- data error: ticker_24h raising, and spread missing for 50% of the
  previously eligible markets, leave every streak and flag unchanged and
  store a failed run; 49% missing is not a data error; one WARNING per run;
- vs BTC and correlation: hand-computed values on a 31-candle fixture; null
  with 30 candles; null for BTC-EUR;
- migration: float volume 1234567.891 becomes "1234567.89" and spread
  0.12345 becomes "0.1234" (half to even); a second run changes nothing;
- status: one case per kind, and each precedence pair (warming up beats
  failing, failing beats fails, fails beats paused, paused beats held);
- sync: a run updates its row at most once a second; a 429 on the 4h
  backfill stores the failure with next_retry at the next cycle, the next
  cycle retries only 4h, and the market leaves warming up once loaded;
  retention deletes only rows past 30 and 90 days;
- activation: PUT returns before the backfill finishes; without
  confirm_looser it is 409; an ineligible market is refused with its rule;
  deactivation keeps an open position managed;
- API: universe returns every market with one status; prices are fetched at
  most once a minute and a failed fetch keeps the old ones; retry answers
  409 during a cycle and for a job that didn't fail, 404 for an unknown job;
- frontend (Vitest): each status kind renders its chip and line; the filter
  survives a reload; the fold rows hide and show; the activate confirm
  shows 960 → 1,056 of 1,000 for the design's numbers;
- e2e: universe.spec.ts on the simulated market: open Universe from the
  sidebar, filter Eligible, activate a market through the confirm, see it
  warming up and then active; Settings → Markets has Open Universe and no
  table; no sideways scroll at 390 px. Wait for the first data as the other
  specs do now, not 5 s (items 016 and 022).

Screenshots (docs/screenshots/026/, next to design/screenshots/): Universe
at 1440 × 900 and 390 × 844, the activate confirm open, Data sync with a
running backfill, the check line after a failed check, Settings → Markets.

Rules:
- Don't change the eligibility thresholds, the streak rules, decisions,
  gates, sizing, exits or the cycle. Nothing activates or deactivates a
  market by itself.
- Bitvavo public endpoints only (/markets, /assets, /ticker/24h,
  /ticker/price, /candles); no trading endpoint, no key (hard rule 1).
- No money arithmetic in the frontend; prices and volumes come as strings.
- Don't edit docs/SPEC.md, design/ or backend/prompts/ (ask in the PR);
  never edit a merged migration.
- Never delete data/, a database or a backup outside a test's own folder;
  the retention step deletes only old sync_runs and market_checks rows.

Done when: CI is green on the last push of each PR and both merge cleanly;
every point of #46 is met and listed under "Issues closed" in PR 2; the
item's file is updated and this kickoff is in docs/kickoffs/archive/.
```

## Why

Marc wants a Universe page like the stock agent's (21 Nov): one place that shows every market the agent could trade and why each one is, or isn't, in play. Today that sits in a table in Settings, without the check result, without what loaded or failed, and without the history of a market's fails. The stock screen shows all of that and has proven useful; crypto needs the same with its own rules.

## Assumptions

- Item 017 (Markets screen) had no kickoff and no code, so it is dropped and replaced by 026 rather than renamed; the screen is called Universe, as in the stock agent and in Marc's request.
- 026 starts after both PRs of 025. It needs 025's settings versions and looser confirmation, and both items change Settings → Markets.
- The data-error rule is new. It copies the stock agent's item 093: without it, one bad ticker response counts a failing day for every market at once.
- Regime is shown for active markets only, because only they have 4h candles loaded; loading them for 236 markets would cost requests for information nobody acts on.
- The 30-day return against BTC and the correlation use simple daily returns on closes, computed at the check, not live.
- A failed backfill retries with the next cycle, which is at most 15 minutes later, so no separate retry timer is needed.

## Open questions

- Should a market that keeps failing for, say, 14 days ask on Needs you to be deactivated, or is today's single alert enough?
- The All view folds plain active markets after 6 rows. With the default 9 that hides some active markets; would Marc rather see every active market in All?
