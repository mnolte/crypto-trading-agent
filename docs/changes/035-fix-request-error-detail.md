---
item: 035
type: fix
title: Request-failure messages drop the exception type
status: building
issue: 80
kickoff: —
spec_prs: []
build_prs: [81]
---

# 035 · fix · Request-failure messages drop the exception type

## 2026-11-25 · build PR #81

During the Bitvavo outage of 25 Nov the Problems card showed
`bitvavo request failed: `, `market context okx/deribit … first error: `
and `clock offset unreadable: bitvavo request failed: ` with nothing after
the colon: httpx timeout exceptions have an empty `str()`, so the message
lost what actually failed.

- `plain_error` moves from `app.agent.market.sync` to `app.errors`, the
  one plain-words formatter for errors in stored rows, log lines and
  messages (hard rule 8); an exception with an empty message yields its
  class name (`ReadTimeout`) instead of a bare colon.
- `BitvavoClient._get` builds `bitvavo request failed: {plain_error(exc)}`;
  the market-context collector's debug line and both `first error:`
  warnings, and the Settings source test (`app/sources.py`), use
  `plain_error` too.

Logging text only: no behaviour, config or prompt change.
