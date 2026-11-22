---
item: 031
type: fix
title: Clean shutdown
status: merged
issue: 62
kickoff: —
spec_prs: []
build_prs: [63]
---

# 031 · fix · Clean shutdown

## 2026-11-22 · build PR #63

Shutdown on Ctrl+C / SIGTERM logged errors: uvicorn cancels requests still
in flight when its graceful-shutdown window expires, and each cancelled
request produced an "Exception in ASGI application" traceback plus
uvicorn's "Cancel N running task(s)" error. The first signal now sets
`app.state.shutdown_requested` before uvicorn starts shutting down, and
`RequestIdMiddleware` ends a cancelled HTTP request quietly once that
flag is set (`CancelledError` still propagates outside shutdown). The
Makefile's `--timeout-graceful-shutdown` went from 10 s to 45 s in `make
dev` and `make run`, above the 30 s SQLite `busy_timeout`, so in-flight
requests finish instead of being cancelled.
