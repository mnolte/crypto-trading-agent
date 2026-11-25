---
item: 036
type: fix
title: Settings e2e save race
status: building
issue: 82
kickoff: —
spec_prs: []
build_prs: []
---

# 036 · fix · Settings e2e save race

`e2e/settings-save.spec.ts` ran on the `w1440` and `w1100` projects against
one shared backend and one SQLite database, while `positions`, `screenshots`,
`universe` and `limit-orders` PUT the whole `/api/settings` object they read
earlier. A concurrent PUT could revert `risk_per_trade_pct` between this
spec's save and its post-reload assertion (PR #71 CI, run 45119010124:
expected `"0.8"`, got `"0.9"`).

The spec now runs once in its own `settings` Playwright project, and the
three viewport projects depend on it, so no other spec's settings write can
interleave. The spec itself is unchanged: it still proves a sectioned save
persists as a numbered version.
