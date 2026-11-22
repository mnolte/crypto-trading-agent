---
item: 028
type: fix
title: Problem count test flake
status: merged
issue: 52
kickoff: —
spec_prs: []
build_prs: [53]
---

# 028 · fix · Problem count test flake

`test_error_activity_stays_out_of_the_legacy_table` submitted two identical
ERROR lines through the async log writer, then waited only until
`unreviewed_problems()` was non-empty — true after the first line committed —
and asserted `count == 2`. When the writer's batches split the two lines, the
read saw count 1 (PR #25 CI, run 44958777266). The test now waits until the
problem's count is 2. No production change.
