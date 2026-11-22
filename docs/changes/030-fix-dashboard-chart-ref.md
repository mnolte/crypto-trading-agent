---
item: 030
type: fix
title: Dashboard chart ref type
status: merged
issue: 59
kickoff: —
spec_prs: []
build_prs: [60]
---

# 030 · fix · Dashboard chart ref type

`PriceChartCard` in `frontend/src/pages/Dashboard.tsx` declared
`anchorRef: React.RefObject<HTMLElement | null>`, but `RefObject<T>` already
types `current` as `T | null`. The extra `| null` made the `<section ref>`
assignment fail `tsc` with TS2322, so `pnpm build` and `make run` broke on
main. The prop is now `React.RefObject<HTMLElement>`, matching what
`useRef<HTMLElement>(null)` returns. Type-only change; no behaviour,
config or prompt change.

## 2026-11-22 · build PR #60

Prop type corrected; frontend typecheck passes again.
