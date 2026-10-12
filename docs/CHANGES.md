# Spec changes

Newest first. Each entry names the sections it touches.

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
