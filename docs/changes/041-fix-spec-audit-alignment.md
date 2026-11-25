---
item: 041
type: fix
title: Spec audit alignment
status: merged
issue: 92
kickoff: —
spec_prs: []
build_prs: [93]
---

# 041 · fix · Spec audit alignment

## 2026-11-25 · build PR #93

Derived money values shown in the frontend now come from the backend as Decimal
strings: Today percent and total open-position unrealised P&L in the snapshot,
the working-order gap percent in paper-order rows, and the Universe immediate-fail
volume threshold in market-check output. The frontend formats these values but no
longer calculates them.

`POST /api/maintenance/prune` clears byte-exact raw LLM brief/request and response
payloads older than 90 days while preserving parsed decisions, checks, costs,
verdicts and outcomes. The endpoint requires the local write token.

`make dev` and `make run` now start Uvicorn through project code that reads
`TA_HOST` and `TA_PORT` through `EnvConfig`. The default bind remains
`127.0.0.1:8000`; Marc approved honoring non-local configured values ahead of the
sign-in/access item.
