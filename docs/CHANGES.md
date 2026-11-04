# Spec changes

Newest first. Each entry names the sections it touches.

## 2026-11-04 — [021] Naming convention for work items

- Every feature, fix, analysis or ops change with a kickoff is a **work item** `NNN-type-slug`, for example `099-fix-stop-retry`.
  - One three-digit sequence for all types, continuing the build plan: 001–020 are build plan items 1–20 ("PR 14" in the spec is item 014).
  - Types: `feat`, `fix`, `analysis`, `ops`, `spec`.
- The name is used for kickoff files, `spec/NNN-slug` and `type/NNN-slug` branches, `[NNN] type: Title` PR titles, commits, `docs/screenshots/NNN/` (from item 021) and changelog headings.
- **Register:** `docs/work-items.md` defines the format and lists every item with its kickoff, spec PRs, build PRs and status, plus the merged work from before the convention. It is not a protected path, so Devin can update the build columns.
- `docs/README.md` rules 5 and 8, the PR description template and "When the spec changes", `AGENTS.md` → How to work, and `CLAUDE.md` follow it. Existing kickoffs (`pr-14.md`, `pr-15.md`) keep their names.
- The convention itself is item 021 (`spec`).

## 2026-11-04 — v0.3: error handling and logs in SQLite

- v0.3 → Logging, errors and the Logs screen, rewritten:
  - logs are stored in `data/logs/logs.db` (tables `log_lines` and `log_problems`, full-text search); the JSON log file is gone, a fallback file is written only while the store can't be written; retention per level (DEBUG 7 days, INFO 30, WARNING 180, ERROR and CRITICAL 365) and a 2 GB cap;
  - error handling for the whole repository: exception classes in `app/errors.py`, isolation boundaries as the only broad catches, `spawn()` for every background task with restart and backoff, timeouts on every external call, one `retry_call()` with the retry rules the spec already had, Python warnings logged and failing tests;
  - an event registry (`app/logevents.py`) with meaning and what to do, and a failure catalogue with the defined outcome, level and event of every known failure;
  - the error pages move from PR 20 to PR 15: the API error shape with a status table, `X-Request-Id`, the in-app pages (not found for a page and a record, something went wrong, the app isn't answering, offline) and the static pages in `deploy/errors/` (app down, updating); frontend errors and console warnings are reported;
  - the Logs screen gains the trace view, event meaning and what to do, Copy error id, a storage footer and ways in from Needs you, the Cycles card and the Decision drawer;
  - the `errors` table is renamed `errors_legacy` and imported once into `logs.db`.
- v0.3 → Shell and Dashboard → Needs you: new item "the app is degraded" (from PR 15).
- v0.3 → "Sign-in, access and error pages" becomes "Sign-in and access" (PR 20 keeps sign-in and adds the 401 and 403 rows).
- Settings, secrets and persistence → Operations, User features → Readiness, Build order (v0.3) and Decisions: pointers to the new section.
- New kickoff `docs/kickoffs/pr-15.md`. Design: `design/CHANGES.md`, entry "Logs screen and error pages".

## 2026-11-04 — v0.3: cycles for a 24/7 market

- v0.3 → Shell and Dashboard: the cycle strip becomes the Cycles card with a rolling 24-hour timeline (slot states and precedence, missed and paused slots, scheduled jobs, summary); nothing counts per Amsterdam day. The scheduler stores skipped slots with their reason. New endpoint `GET /api/cycles/recent?hours=24`.
- "Today's decisions" becomes "Decisions · last 24 hours" (rolling, not since midnight).
- docs/kickoffs/pr-14.md updated to match.

## 2026-11-03 — v0.3: screens and tools from Stock Trading Agent

- New section "v0.3: screens and tools from Stock Trading Agent":
  - Shell and Dashboard: the behaviour behind the aligned design (header, Needs you with `GET /api/needs`, cycle strip, Today's decisions, Markets card, phone width, interaction rules);
  - Logging, errors and the Logs screen (replaces the Logs and Errors bullets under Operations; new Readiness error criterion);
  - Positions & orders, Markets screen, Strategies screen (with holding BTC and the static BTC + cash benchmark), Backtest (model cutoff rule, own €5 cap, training and test), Sign-in, access and error pages;
  - Build order: PR 14 to PR 20.
- GUI screens → Visual system: layout from 390 px once PR 14 lands.
- Decisions: design and screens follow Stock Trading Agent.
- Design: see `design/CHANGES.md`, entry "Aligned with Stock Trading Agent".
- Workflow: spec and design changes now arrive as pull requests with the `spec-change` label (see `CLAUDE.md`); kickoffs go in `docs/kickoffs/pr-<n>.md`.

## 2026-10-25 — Strategy families tested

- Decisions: breakout, rotation and dip-buying families failed the rule; the search for
  price-based strategies stops. Open item: test the positioning data in February 2027.
- docs/RESEARCH.md: new section "Strategy families". scripts/regime_history.py: part 6
  (--families).

## 2026-10-24 — PR 13 resumed

- Decisions: PR 13 (prompt v2, brief version 2) goes ahead; PR 10 and 11 stay paused.

## 2026-10-23 — Strategy research and pause

- Decisions: strategy research result (no active approach beat a static BTC + cash mix
  at the same drawdown after costs) and the rule for future strategy changes. PR 10, 11
  and 13 paused; PR 12 only if nearly done; paper agent, v0.2 collector and fix PR continue.
- New: docs/RESEARCH.md (the findings) and scripts/regime_history.py (the research script).

## 2026-10-22 — v0.2: prompt v2 (decision quality)

- New subsection "Prompt v2: decision quality": brief versions declared in the prompt
  header; version 2 brief fields (`previous_decision` outcome, failed gates and repeats;
  `trade_math` with stop zone, stop and target candidates, workable pairs with R/R,
  `buy_feasible` and the 1h conditions); output schema v2 (`stop_basis`, `target_basis`,
  enum signal names, levels null unless BUY); v2 post-checks; upgrade of stored v1
  briefs for replays; validation by replay and the switch criteria.
- New prompt file `backend/prompts/decision.v2.md` (written by Marc).
- The sentiment prompt of step 2 is now `decision.v3` (v2 plus the sentiment block);
  Shadow A compares v3 with live on v2. Shadow B uses the live prompt version.
- Build order: PR 13, before PR 10.

## 2026-10-21 — v0.2: limit entries (Shadow B)

- New subsection "Limit entries, tested as Shadow B": Entry orders setting (Market /
  Limit post-only), maker fee (default 0.15%), stops and forced exits always at market,
  fee gate with limit entries (0.45% costs → stop ≥ 1.5%), post-only placement, fill only
  when price trades through the limit, cancel at the next cycle or when a gate blocks,
  new `unfilled` status with tracked outcome, `paper_orders` table, What-if stays on
  market orders, Shadow B (decision.v1 + limit entries), decision rule.
- Build order: PR 12.

## 2026-10-14 — v0.2: positioning data

- New section "v0.2: positioning data" before "After v0.1": step 1 collector (OKX
  funding, open interest, long/short ratio; Deribit DVOL and 25-delta skew; BTC and
  ETH, hourly, no effect on decisions), step 2 `sentiment` brief block and
  `decision.v2.md` tested as Shadow A, step 3 promotion rule, news flag and macro
  calendar, build order PRs 9–11, and what stays out of v0.2.
- "After v0.1": the first group now points to v0.2.
- Step 2 GUI: the sentiment block sits below the signal stack in the Decision drawer, which also gains News flag and Macro event gate rows (matches design/CHANGES.md 2026-10-14).

## 2026-10-11 — Shadows and What-if

- User features → Shadow strategies: briefs come from the same cycle snapshot as live
  with the shadow's own position and account block (so shadows can SELL); the model is
  called when the pre-filter passes or the shadow holds the pair; gates use the shadow's
  own account. A new shadow starts flat with cash equal to live equity at creation;
  Compare measures live from that moment. At 80% of the cost cap a shadow makes no new
  decisions (no Rules stand-in), the watcher keeps managing its positions, and the pause
  window is left out of the comparison.
- User features → What-if: vetoed, expired and pending decisions never fill in a
  scenario; a scenario can block a real fill or fill a gate-skipped decision.

## 2026-10-11 — Pauses, plain briefings and the quiet line

- User features → Per-coin pause: a paused pair with no open position gets no model
  call (shadows included); Rules answers and BUY ends as SKIP at the Entry pause gate,
  which is checked again when a pending approval fills. With an open position the model
  is called as usual.
- User features → Daily briefing: no OpenAI key, a failed call or the monthly cost cap
  reached → plain template from the computed facts, labelled with the reason; card and
  email still appear. Weekly reviews follow the same rule.
- User features → "Why so quiet?": active brakes and pauses are appended, global first
  (shock brake, pause on all entries, then per-coin pauses).

## 2026-10-11 — SELL outcome: definition of 1R

- Outcome tracking → Labels: for a SELL, 1R is the position's original risk per unit
  (entry fill price minus the stop set at entry), not the stop at exit time. "After
  24h" is the close of the last closed 1h candle at exit + 24h vs the exit fill price.

## 2026-10-11 — ADX wording

- Indicator definitions → Reads: "ADX above 25" is now "ADX at 25 or more (≥ 25)",
  matching the regime, Rules condition C6, the signal-claims check and Insights. The
  threshold is inclusive everywhere (`>=`). No behaviour change for code that already
  uses `>=`.
- Settings, secrets and persistence: environment variable names (TA_HOST, TA_PORT,
  TA_DB_PATH, TA_DATA_DIR) and backups inside the data folder are now in the spec.

## 2026-10-10 — First handoff

SPEC.md as exported on 10 October 2026, after two full reviews. It already includes:

- Risk gates: stops 0.5–5 × ATR(1h) with the fee gate deciding SKIP; exposure cap 100%;
  open risk counts max(0, entry − stop); correlated positions, shock brake and risk
  throttle; candle age measured from the close.
- OpenAI decision layer: regime block, portfolio block and limits in the brief; output
  schema with nullable levels and an invalidation price; post-checks and regime rules in
  code; temperature 0; monthly € cost cap and per-feature call limits.
- Supervised and manual approvals expire at the pair's next cycle; R/R, size and gates are
  recomputed at fill. Cycles are fixed at 15 minutes.
- Data model and API: all snapshot types defined; WebSocket sends snapshot and alert
  messages; endpoints for activity, saved What-if scenarios, shadow promotion, Ask
  conversations, prompt versions and pruning.
