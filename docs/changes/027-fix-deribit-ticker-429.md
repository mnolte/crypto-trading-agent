---
item: 027
type: fix
title: Deribit ticker 429 rate limit
status: building
issue: 47
kickoff: 027-fix-deribit-ticker-429.md
spec_prs: []
build_prs: [57]
---

# 027 · fix · Deribit ticker 429 rate limit

Fixes the hourly `skew_25d` collection hitting Deribit's public rate limit:
`_collect_deribit` fires ~216 per-instrument `/public/ticker` requests at
concurrency 4 with a single retry, so 429 bursts drop every in-flight
instrument. The build paces the requests and gives 429 a real retry budget;
no change to skew values, instruments or stored rows. Root cause and
proposed fix: issue #47, kickoff `027-fix-deribit-ticker-429.md`.

## 2026-11-22 · build PR #57

- New setting `market.deribit_requests_per_second` (default 2, range 0.1–20,
  off the Settings screen): a shared minimum-interval pacer in the collector
  spaces every Deribit request — DVOL pages and ticker calls alike — so the
  gather cannot burst past Deribit's public limits. OKX is unchanged.
- Collector requests answered 429 or 5xx now get up to 5 attempts with
  exponential backoff and jitter, honouring `Retry-After` (still capped at
  30 s); network errors keep their single retry.
- Per-request failures log at debug; each source logs one summary warning
  per collect instead of one warning per instrument. The source error is
  still recorded once per collect.
- Deviates from `docs/SPEC.md` → Market context collector, which says "one
  retry on a network error, 5xx or 429": 429/5xx now have the larger budget
  above. Skew values, instruments selected, stored rows and the hourly
  cadence are unchanged.
