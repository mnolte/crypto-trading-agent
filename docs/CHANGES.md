# Spec changes

Newest first. Each entry names the sections it touches.

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
