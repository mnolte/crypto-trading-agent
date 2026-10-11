# Spec changes

Newest first. Each entry names the sections it touches.

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
