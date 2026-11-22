---
item: 032
type: ops
title: Startup self-healing
status: merged
issue: 64
kickoff: 032-ops-startup-self-healing.md
spec_prs: []
build_prs: [65]
---

# 032 · ops · Startup self-healing

## 2026-11-22 · kickoff

Added the work item and kickoff after reviewing startup recovery. The
current application restores the paper account, positions, pending-decision
state, kill/shock/throttle state and cycle numbering; starts paused; marks
stale What-if work interrupted; checks the trading database after an
unclean shutdown; and runs supervised background jobs. Market-data and
price refresh failures are isolated from startup.

The remaining work is to turn these behaviours into an explicit recovery
contract. A failed migration or database integrity check must not be
treated as a successful start, and backup-aware recovery must not silently
overwrite or delete the primary database. The kickoff requires pass and
fail tests for clean, unclean, corrupt and migration-failure starts, while
preserving paper-only operation, restart-paused state, persisted positions
and fail-closed behaviour.

## 2026-11-22 · build PR #65

Startup now has an explicit fail-closed recovery contract. Before Alembic
runs, the trading database is checked with `PRAGMA quick_check`; a missing
file is still created, but a corrupt or unreadable database logs
`db.corrupt` and stops startup before migrations or engine construction,
leaving the database untouched for inspection or manual restore. A
migration failure logs `db.migration_failed` and raises
`StartupRecoveryError`, so the process exits instead of serving a
half-migrated state.

Unclean and clean shutdowns now close the same persisted in-progress work:
queued/running What-if rows are marked `interrupted`, running `sync_runs`
are failed with the interruption reason appended, and unfinished
`market_checks` are failed. The cleanup is per-table and idempotent, so a
legacy database missing one table does not block the others. Startup also
reports integrity problems in `logs.db` and the newest daily backup without
blocking on them (`logs.corrupt`, `backup.invalid`).

`WhatIfJobOut.status` now includes `interrupted`, matching the state
startup already wrote; `openapi.json` and the frontend types were
regenerated. No automatic restore was added: a corrupt primary database
requires an operator to preserve `data/` and restore a known-good backup or
repair the file.
