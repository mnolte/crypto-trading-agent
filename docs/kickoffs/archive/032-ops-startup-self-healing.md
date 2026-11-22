# 032 · ops · Startup self-healing

Kickoff for Devin. Start the session with: "Build docs/kickoffs/032-ops-startup-self-healing.md."

```text
032 (Startup self-healing). Goal: make the application's startup recovery
explicit, safe and testable. Assess and harden what happens when the app
starts after a clean shutdown, an unclean shutdown, a database integrity
failure, or a failed migration, while preserving paper-only operation,
restart-paused state and fail-closed behaviour. Start from the latest main.

Names (docs/work-items.md): branch ops/032-startup-self-healing, PR title
"[032] ops: Startup self-healing", commits start with [032]. This kickoff
PR adds docs/kickoffs/032-ops-startup-self-healing.md and
docs/changes/032-ops-startup-self-healing.md. Put `Closes #64` in the
PR description (issue mnolte/trading-agent#64). If the build is split,
the first build PR moves this kickoff unchanged to docs/kickoffs/archive/
with git mv and uses `Part of #64`; the last uses `Closes #64`.

Read first:
1. docs/changes/032-ops-startup-self-healing.md and issue #64.
2. docs/SPEC.md → Settings, secrets and persistence (restart behaviour,
   single engine, sleep/wake and operations), Testing and acceptance
   criteria, and the Operations section.
3. backend/app/main.py → lifespan, _unclean_start,
   _interrupt_running_runs, refresh_market_data and shutdown cleanup.
4. backend/app/db.py → EngineLock, run_migrations and make_engine.
5. backend/app/agent/engine.py → start, _recover_startup,
   refresh_prices, _backfill_stopped_slots and transaction recovery.
6. backend/app/agent/broker/paper.py → current-account and position loading.
7. backend/app/operations.py → backup_database and backup_loop.
8. backend/tests/test_shutdown.py, backend/tests/test_engine.py,
   backend/tests/test_operations.py and the migration/config tests.
9. AGENTS.md → hard rules, especially paper-only, money as Decimal, no
   real network calls in tests, and local-only binding.

Current assessment to preserve and extend:
- The lifespan acquires the database lock before migrations, creates the
  trading DB directory, and runs Alembic to head.
- A leftover lock file triggers _unclean_start: it logs, runs
  PRAGMA quick_check on agent.db, marks stale What-if runs interrupted,
  and continues startup. quick_check is diagnostic only; there is no
  automatic backup selection or recovery path when it fails.
- Settings load/validate, the market client and source registry are
  created, the current paper account and positions are restored, stale
  pending decisions and entry orders are expired/cancelled, latest
  decisions are rebuilt, kill/shock/throttle state is restored, and the
  engine starts paused.
- Startup market data and price refresh are isolated from startup; a
  failing source records a source failure and startup continues.
- Background tasks use the supervised spawn helper with bounded restart
  and crash-loop reporting; daily/backup/context/outcome jobs are started.
- Shutdown is graceful and checkpointed, with interrupted What-if cleanup.
- Daily SQLite backups exist, but startup does not verify a usable backup
  or define recovery when the primary DB is corrupt or migrations fail.

Build, in this order (one focused commit per step where useful):
1. Add a startup-recovery contract to the issue/change file: enumerate the
   states that must be handled and the observable result for each. Decide
   and document whether migration failure and DB corruption fail closed
   before accepting writes, and which backup may be selected. Do not guess
   a destructive restore policy; put unresolved choices in the PR and ask
   Marc before implementing them.
2. Harden the safe, non-destructive parts first: make startup diagnostics
   cover the trading DB, logs DB and latest backup where practical; ensure
   quick_check/integrity failures produce a clear critical log and prevent
   the engine from accepting work while preserving the database for
   inspection; ensure migrations are never retried against a known-corrupt
   database; and make unclean-start interrupted-state cleanup idempotent
   and observable.
3. Implement backup-aware recovery only after the issue/PR documents the
   exact selection and safety rules. Prefer a new isolated recovery copy
   or explicit operator-approved restore; never silently overwrite or
   delete the primary database, data directory, backups or logs. Preserve
   hard rule 1 (paper only) and hard rule 2 (Decimal money).
4. Add pass and fail tests for every recovery gate. Use only temporary
   test databases, injected clocks/sleep and mocked network/market clients.
   Cover clean start, leftover lock, corrupt DB, failed migration,
   interrupted What-if/run state, expired pending decisions, restored
   positions and fail-closed API/engine behaviour as applicable. Tests
   must not depend on test ordering or shared databases.
5. Run only the changed-area checks locally (ruff on changed files and
   the relevant backend test files). Push early as a draft PR; wait for CI
   and fix failures on the branch. Do not run the full suite locally.

Do not:
- add exchange trading endpoints, API keys or live-order capability;
- change paper accounting or risk thresholds without a spec decision;
- silently delete/rename/overwrite agent.db, backups, logs or data/;
- treat a failed quick_check as permission to continue trading;
- edit docs/SPEC.md, design/ or prompt versions in use;
- run make test, the full backend/frontend/e2e suites, soak, audits or
  secret scans locally.

In the PR description include: what startup states are now handled; what
is still manual; exact tests and CI evidence; deviations from the spec;
design differences (none expected); and “Issues closed” with each point of
#64 checked against file:line or a test.
```

## Why

Startup is the point where persisted paper state, risk controls and
background work are reconstructed. The current code already restores
positions and risk state and starts paused, but its database-corruption
and migration-failure paths are not a complete self-healing contract, and
it does not automatically use its daily backups. A small, explicit
recovery item prevents a future change from treating diagnostics as a
successful recovery or silently losing state.

## Assumptions

- “Self-healing” means safe automatic recovery where possible and a clear,
  fail-closed manual path where automatic recovery would be unsafe.
- The first implementation must not invent a destructive restore policy.
- The issue and kickoff define the work item; implementation decisions that
  change behaviour still need review.

## Open questions

- Should a corrupt primary database be restored automatically from the
  newest valid backup, or should startup stop and show an operator-facing
  recovery instruction?
- Which data directories and databases must pass integrity checks before
  the app is considered healthy?
