---
item: 039
type: fix
title: Outcome inactive backfill
status: merged
issue: 87
kickoff: —
spec_prs: []
build_prs: [89]
---

# 039 · fix · Outcome inactive backfill

## 2026-11-25 · build PR #89

Deactivating a market stops its cycle candle refreshes, but decisions made
before deactivation still need up to 72 hours of closed 1h candles for their
outcomes. Without those candles the outcome worker exhausted its retries and
marked the outcomes unavailable, losing the missed/avoided evidence the
scorecard and Insights use.

The outcome worker now backfills only the missing outcome windows through the
market client before computing outcomes. Fetches are grouped per pair because
the 4h, 24h and 72h windows are nested; they write candles only and never
activate the pair or run its decision cycle. A fetch failure is logged as a
candle fetch failure and the existing outcome retry and terminal-failure path
continues unchanged. Terminal failures caused by missing candles reopen when
the candles become available, and the worker's startup pass gives those rows
one bounded backfill attempt instead of retrying them forever every hour.
