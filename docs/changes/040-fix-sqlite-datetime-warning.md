---
item: 040
type: fix
title: SQLite datetime deprecation warning
status: merged
issue: 90
kickoff: —
spec_prs: []
build_prs: [91]
---

# 040 · fix · SQLite datetime deprecation warning

## 2026-11-25 · build PR #91

Startup and shutdown recovery used textual SQL for interrupted sync runs and
market checks. Because textual SQL does not infer column types, their datetime
parameters reached sqlite3 through Python 3.12's deprecated default datetime
adapter and startup logged a deprecation warning.

The recovery updates now bind those parameters explicitly as SQLAlchemy
`DateTime` values. SQLAlchemy formats them through its SQLite datetime bind
processor before they reach sqlite3, preserving the stored format and the
interruption behavior while removing the deprecated adapter from this path. A
regression test captures warnings from the recovery path and fails if the
default datetime adapter warning returns.
