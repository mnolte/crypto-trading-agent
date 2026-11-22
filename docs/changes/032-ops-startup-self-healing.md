---
item: 032
type: ops
title: Startup self-healing
status: ready
issue: 64
kickoff: 032-ops-startup-self-healing.md
spec_prs: []
build_prs: []
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
