---
item: 038
type: fix
title: Daily check invalid market
status: merged
issue: 86
kickoff: —
spec_prs: []
build_prs: [88]
---

# 038 · fix · Daily check invalid market

## 2026-11-25 · build PR #88

Bitvavo can list a market in `/markets` while its candles endpoint rejects it;
on 25 Nov `CLEAR-EUR` was listed in `auction` status while
`/CLEAR-EUR/candles` returned HTTP 400. That one market aborted the startup
eligibility check after 209 of 424 markets, so the whole `daily_check` sync run
was stored as failed.

`run_eligibility_check` now contains a `MarketDataError` to the market that
raised it: the market keeps its categorical status/type evaluation with missing
numeric metrics, the check continues over the remaining EUR markets, and the
startup or retry sync run records the market in `failures`. A completed check
with such a failure is `partial`; the existing whole-check data-error behaviour
for `/markets`, `/ticker/24h` and widespread missing volume/spread data is
unchanged.
