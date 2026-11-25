---
item: 037
type: fix
title: Deribit skew collection still hits 429 rate limits
status: merged
issue: 84
kickoff: —
spec_prs: []
build_prs: [85]
---

# 037 · fix · Deribit skew collection still hits 429 rate limits

## 2026-11-25 · build PR #85

Item 027 paced the option-chain `/public/ticker` polling to 2 requests per
second and gave 429 responses a retry budget, but the hourly collect still
polled about 218 option instruments and a small number of tickers kept
exhausting that budget. The collector now keeps the same expiry choice,
instruments, skew maths, stored rows and hourly cadence, but takes the
per-instrument ticker data from Deribit's `incremental_ticker` websocket
channel instead of REST polling: one short-lived connection per symbol, one
batched subscription, the first full snapshot per instrument, then close.
DVOL and instrument discovery stay on the paced REST endpoints. Connection,
subscription and incomplete-snapshot failures follow the existing collector
error path and write no new skew row. `websockets` becomes a direct
dependency (it was already present through `uvicorn[standard]`).
