---
item: 042
type: fix
title: Markets regime chip overflow
status: merged
issue: 94
kickoff: —
spec_prs: []
build_prs: [95]
---

# 042 · fix · Markets regime chip overflow

## 2026-11-25 · build PR #95

The Dashboard Markets table now uses the compact regime chip from the design:
trend and volatility only in the row, with the regime age and new-regime note in
the chip title. The regime grid cell clips its contents and the chip ellipsizes
within its column, so a long regime label cannot paint over the Rules column.
Other regime-chip uses keep the full text with the age.
