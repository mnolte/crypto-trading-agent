---
item: 034
type: fix
title: Universe API query storm — per-market decision lookups
status: building
issue: 68
kickoff: —
spec_prs: []
build_prs: [69]
---

# 034 · fix · Universe API query storm — per-market decision lookups

## 2026-11-23 · build PR #69

`GET /api/universe` never answered: the Universe screen (and every other
endpoint) appeared empty. A faulthandler dump of a live instance showed
the event loop inside `repo.latest_decisions`, called once per market
from `market_status` — ~860 scans of the 182 MB `decisions` table per
request, all synchronous on the loop, while the frontend refetched every
60 s and on every snapshot push.

The fix:

- `repo.latest_decisions` fetches the newest decision per pair in two
  queries (a `MAX(at)` group-by joined back to `decisions`) instead of
  one `ORDER BY … LIMIT 1` per pair; migration 0017 adds
  `ix_decisions_account_pair_at` so both steps are index lookups.
- `market_status` takes the prefetched `decisions` and `positions` maps
  from `get_universe` instead of re-querying per market; without them it
  keeps the old per-market lookups for tests and other callers.
- `get_universe` computes `candle_counts` only for active markets (the
  only rows that can be warming up) and runs the whole DB section in a
  thread (`asyncio.to_thread`), so slow queries can't starve HTTP.

Measured on the production database copy: `/api/universe` went from
>120 s (no answer) to 0.88 s for all 429 markets.
