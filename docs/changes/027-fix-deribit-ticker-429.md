---
item: 027
type: fix
title: Deribit ticker 429 rate limit
status: ready
issue: 47
kickoff: 027-fix-deribit-ticker-429.md
spec_prs: []
build_prs: []
---

# 027 · fix · Deribit ticker 429 rate limit

Fixes the hourly `skew_25d` collection hitting Deribit's public rate limit:
`_collect_deribit` fires ~216 per-instrument `/public/ticker` requests at
concurrency 4 with a single retry, so 429 bursts drop every in-flight
instrument. The build paces the requests and gives 429 a real retry budget;
no change to skew values, instruments or stored rows. Root cause and
proposed fix: issue #47, kickoff `027-fix-deribit-ticker-429.md`.
