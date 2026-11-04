# Error-handling audit — 2026-11-04

Measured on `main` (`60eaaa8`, PR 14 merged). For each `try`/`except`, background
task, `print()` and silent failure path: what happens today and the fix — a row
of the spec's failure catalogue, or a question at the end.

## 1. `try`/`except` inventory

54 `except Exception` in 14 files; no bare `except:` anywhere. Listed per file,
with narrower catches where they matter.

### `backend/app/agent/engine.py` (20)

| Line | What happens today | Fix |
|---|---|---|
| 289 | `refresh_prices` per pair: fetch fails → `log.exception`, rollback, fall back to stored candles | Keep (isolation boundary: startup price refresh per pair); log through the registry event `job.failed`-equivalent: `candles.fetch_failed` at WARNING |
| 306 | outer catch of the same: whole refresh dies → `log.exception`, startup continues | Keep; WARNING `job.failed` |
| 389, 412 | `pause`/`stop & flatten` paths: commit fails → rollback, restore in-memory state, re-raise | Keep — rollback-then-re-raise is the defined pattern; add ERROR `db.locked` / `db.unwritable` when the commit itself fails |
| 499 | `_record_missed_slots`: any failure → `log.exception`, loop continues | Keep; narrow so only storage failures land here, event `db.locked`/`job.failed` |
| 519 | `_scheduler_loop`: a crashed cycle → `_note_cycle_error` | Keep — boundary "one cycle"; log ERROR `cycle.step_failed` with `error_id`, store the slot state |
| 544 | `_note_cycle_error`'s own record fails → `log.exception` | Keep; second-order failure still logged once |
| 552 | `_watcher_loop`: a crashed tick → counted, `log.exception`, `_note_watch_error` on 1st and every 10th | Keep — boundary "one watcher pass"; ERROR `watcher.unpriced`-family event |
| 576 | `_note_watch_error`'s own record fails → `log.exception` | Keep |
| 624 | `guarded(pair)`: a pair crash → `_record_pair_failure` | Keep — boundary "one pair in a cycle"; ERROR `cycle.pair_failed` with `error_id` |
| 668 | `_measure_clock_offset`: `except Exception: pass` — **silent** | Catalogue row `clock.offset_failed` (WARNING) and `clock.offset_high` over 2 s |
| 702 | `_record_pair_failure` nested: decision-record insert fails → rollback only, **silent** | Log ERROR `db.unwritable`-family once; never re-raise |
| 739, 745 | `_load_context`: `except MarketDataError: pass` — BTC/USDC context silently absent | Expected condition → WARNING `candles.fetch_failed` naming BTC-EUR / USDC-EUR |
| 789 | pair candle fetch: `except MarketDataError` → `candles = {}` → stale-pair path | Defined outcome; log WARNING `candles.fetch_failed` / `candles.stale` (ERROR when every active pair fails in one cycle) |
| 960, 977 | fill paths: commit fails → rollback, restore, re-raise | Keep; `db.locked` ERROR on the write failure |
| 1147 | `_post_only_limit` order-book fetch: `except Exception: return None` — **silent** | Catalogue row `book.unavailable` (WARNING); narrow to `MarketDataError` |
| 1465 | invalidation re-eval per pair: crash → `_record_pair_failure` | Keep — same boundary as 624 |
| 1495, 1709, 1825 | watcher-tick / execution commits fail → rollback, restore, re-raise | Keep; the re-raise lands in the loop boundary (552) |
| 1514 | `_priced`: `except MarketDataError` → price 0 → cached-price fallback | Keep; feeds `watcher.unpriced` WARNING → CRITICAL after 2 min |
| 2216 | `_notify`: `except Exception: return` — notification failures **silent** | Log ERROR `notify.email_failed` / DEBUG for browser sends; the dispatcher already isolates channels |

### `backend/app/features/shadows.py` (9)

| Line | What happens today | Fix |
|---|---|---|
| 226 | per-shadow-pair decide crash → `log.exception` | Keep — boundary "one pair in a shadow"; `cycle.pair_failed` |
| 238, 241 | shadow cycle commit / pass crash → rollback + `log.exception` | Keep — boundary "one shadow" |
| 342 | shadow signals fail → `log.exception`, pair skipped | Keep; `cycle.pair_failed` |
| 355 | shadow BTC-regime compute: `except Exception: pass` — **silent** | Narrow to expected data failures; WARNING `candles.fetch_failed` or DEBUG when data merely absent |
| 595 | shadow order-book fetch: `except Exception: return None` — **silent** | `book.unavailable` WARNING; narrow to `MarketDataError` |
| 938, 941 | shadow watcher pass/commit crash → rollback + `log.exception` | Keep — boundary "one watcher pass" (shadow) |
| 961 | shadow re-eval crash → rollback + `log.exception` | Keep |

### `backend/app/features/whatif.py` (4)

| Line | What happens today | Fix |
|---|---|---|
| 232 | stored plan won't parse → `pass`, falls through to raw-response parse | Expected condition → DEBUG `whatif.plan_parse` (data may legitimately have no plan) |
| 519 | signal computation fails for one replayed pair → `log.exception`, pair skipped | Keep — boundary "one backtest/what-if strategy step" |
| 565 | stored plan parse for a target → `target = None` | Same as 232 — DEBUG |
| 1080 | whole run fails → `log.exception`, row `failed` with the message | Keep — job boundary `job.failed` |

### Other files

| File:line | What happens today | Fix |
|---|---|---|
| `agent/openai_client.py:180` | `response.output_text` accessor raises → `text = None` → output-parse path | Narrow: only SDK attribute errors expected; log nothing (handled downstream as invalid output) |
| `api/notifications.py:191, 243`, `api/routes.py:321` | WebSocket send fails → client discarded | Expected condition (closed socket) → DEBUG `ws.client_lost` |
| `api/notifications.py:217` | `_deliver_email` fails → writes activity "notification delivery failed" | Keep — boundary "one notification channel"; ERROR `notify.email_failed` |
| `config.py:76` | keyring read fails → `warnings.warn`, env fallback | Catalogue row `secrets.unavailable` (ERROR): keys count as not set + Needs you key item |
| `config.py:88, 99` | keyring write/delete fails → `SecretStoreError` raised | Convert at the client boundary into `app.errors.ConfigError`-family (keeps `SecretStoreError` as the app-level type) |
| `context/collector.py:264` | one retry on network/timeout after 0.25 s; 429/503 honour `retry-after` (≤30 s) | Keep the policy; log each attempt DEBUG `retry.attempt`; failure → `job.failed`/source record |
| `context/collector.py:280` | `_try` per source: `except Exception` → `log.warning`, returns None | Keep — boundary "one collector source"; narrow once library exceptions are converted (`SourceError`) |
| `context/collector.py:302` | per-source collect crash → source registry + `log.exception` | Keep — same boundary |
| `context/scheduler.py:32` | `context_loop` catches everything, logs, **then the loop is dead** — collector never runs again | `spawn()` with restart+backoff (`task.crashed` / `task.crash_loop`) |
| `features/ask.py:296` | `get_indicators_at` tool fails → `{"error": ...}` returned, **not logged** | Boundary "one ask tool": log ERROR `job.failed`-family + keep the returned error |
| `features/ask.py:450` | any tool raises → `log.exception` + `{"error": ...}` | Keep |
| `features/ask.py:557` | ask prompt file unreadable → reason "ask prompt missing" | Expected condition → WARNING `config.invalid`-family |
| `features/briefings.py:629` | briefing/review prompt unreadable → `prompt = None` + reason | Same WARNING |
| `features/briefings.py:650` | model-call transport blows up → `CallResult(status="api_error")` | Keep; ERROR `model.call_failed` |
| `features/briefings.py:792` | `briefing_loop` tick crash → `except Exception: pass` — **silent** (comment claims "logged next tick"; nothing logs it) | Boundary "one job iteration": ERROR `job.failed`; also runs under `spawn()` so the loop itself restarts |
| `features/needs.py:338` | needs assembly fails → `log.exception`, empty Needs | Keep — never breaks a snapshot |
| `main.py:130` | `refresh_market_data` fails → source registry records "bitvavo" | Keep — boundary "one job"; today a *database* error here would be recorded as a Bitvavo failure — distinguish `StorageError` from `SourceError` |
| `main.py:151` | `daily_market_loop` job crash → `log.exception`, loop lives | Keep |
| `repo.py:85` | settings-history write fails → `pass` — **silent** | WARNING `job.failed`-family ("settings saved; history row could not be written") |
| `config.py:97` | `PasswordDeleteError` → `pass` | Expected — deleting a key that isn't there; DEBUG or `noqa` |
| `api/ws.py:35` | `WebSocketDisconnect` → `pass`, client discarded | Expected — fine as is (narrow already) |
| `api/notifications.py:37` | `SecretStoreError` → 503 | Keep; gets the API error shape |
| `api/routes.py:210,221,256,268` | `KilledRefusal`/`PendingRefusal` → 409 | Keep; map to `conflict` code in the error shape |
| `api/routes.py:278` | `MarketDataError` on close-position → 503 | Keep; `SourceError` conversion |
| `features/briefings.py`, `whatif.py` | narrow `except (json.JSONDecodeError, …)` parses | Expected conditions — fine as is |
| `api/notifications.py:259` | SMTP test → 502 with the exception text | Keep code; message must not echo secrets — scrubbed shape |

## 2. Background tasks

11 bare `asyncio.create_task`, none awaited or supervised:

| Site | Task | On failure today |
|---|---|---|
| `main.py:230` | `_resolve_source_error_after_recovery` | exception vanishes (task result never read) |
| `main.py:281` | `refresh_market_data` (startup) | vanishes |
| `main.py:284` | `daily_market_loop` | crashes once → logged, loop dead |
| `main.py:287` | `context_loop` | crashes once → logged, loop dead |
| `main.py:289` | `outcome_loop` | vanishes — outcome backfill stops silently |
| `main.py:290` | `backup_loop` | vanishes |
| `main.py:292` | `briefing_loop` | vanishes |
| `engine.py:182` | `_scheduler_loop` | vanishes — the agent stops cycling silently |
| `engine.py:183` | `_watcher_loop` | vanishes — **exits stop being watched silently** |
| `notifications.py:104` | `notify()` dispatch | vanishes |
| `notifications.py:132` | `_deliver_email` | handled inside the coroutine itself |

Fix for all: the `spawn()` helper — copies log context, `task.crashed` ERROR with
traceback, restart after 5 s→5 min backoff, CRITICAL `task.crash_loop` after 5
crashes in 10 minutes (Needs you degraded item). Notification one-shots use
`spawn()` without restart (they are fire-and-forget, not loops).

## 3. `print()`

- `backend/app/context/backfill.py:29,36,37` — a CLI maintenance script run by
  hand; `print` is its interface. **Stays.** It gets a module logger too so its
  work also lands in `logs.db` when run inside the app process.
- `backend/app/tools/compare_prompts.py:329,336,353` — same class of CLI
  script. **Stays.**

## 4. Logging today, per component

`operations.configure_logging` installs one `RotatingFileHandler` (JSON lines,
10 MB × 5, `data/logs/app.log`) at INFO on the root logger. There is **no
console handler** — `log.exception` output is invisible in `make dev` output —
and no scrubbing, no event codes, no trace context. Only 9 of 61 modules under
`backend/app` have a logger: `agent/engine.py`, `context/collector.py`,
`context/scheduler.py`, `features/ask.py`, `features/needs.py`,
`features/shadows.py`, `features/whatif.py`, `main.py`, `operations.py`.
Uvicorn logs to its own console only.

INFO and DEBUG needed per component (registry codes in step 3 of the PR):

| Logger | INFO milestones | DEBUG steps |
|---|---|---|
| `app` | startup (version, commit, config hash, data dir, Python), `app.unclean_shutdown`, shutdown requested/complete | settings loaded, lock held/released |
| `agent.scheduler` | `cycle.start`, `cycle.end` (the summary line), `scheduler.slot_skipped` | slot due/missed bookkeeping, next-run computation |
| `agent.watcher` | watcher start/stop | each pass (`watcher.pass`), prices fresh/stale per pair |
| `agent.rules` / `agent.decider` | `decision.final` per pair (action, decider, reason) | pre-filter conditions with values, brief sent (`model.call_sent` with `llm_call` id), `model.call_returned` (tokens, latency), `model.output_invalid`, `postcheck.failed` |
| `agent.risk` | — | gate inputs and results, sizing math |
| `broker.paper` | `fill.entry`, `fill.exit`, `order.placed/filled/cancelled`, stop moved | fill math: reference, slippage, fee |
| `market.bitvavo` | — | `http.call` per request (method, path, status, duration, attempt — no bodies), `retry.attempt` |
| `market.universe` | eligibility transitions, backfill done | funnel counts per rule |
| `context.collector` | collect pass summary per source | per-endpoint calls |
| `notify` | `notify.sent` per channel | suppression/repeat accounting |
| `ops.backup` | `job.start`/`job.end` (file, size, kept) | — |
| `ops.logs` | writer fallback engaged/recovered, retention summary | dropped counts |
| `api` | — | request per call (`request_id`, route, status, duration); `api.rejected` |
| `frontend` | — | `frontend.error` reports |
| `python.warning` | — | every `warnings.warn` |
| `ops.jobs` | each scheduled job start/end/fail | — |

## 5. External calls: timeout and retry today

| Call | Timeout today | Retry today | Spec wants |
|---|---|---|---|
| Bitvavo REST (all endpoints) | 10 s (`bitvavo.py:116`) | 429 → wait until reset header, ≤2 retries; everything else: no retry (one `MarketDataError`) | 10 s ✓; candle staleness retried by the engine 3×10 s (`_fresh_1h`) |
| Newest-1h-candle check | — | 3 tries, 10 s apart (`STALE_RETRIES`) | unchanged ✓ |
| Price stream | n/a — **no WebSocket client exists**; the watcher polls `ticker/price` every 30 s with `max_429_wait_s=2.0` | — | `stream.*` rows apply once a stream exists; today the REST ticker is the stream — its failures surface via `watcher.unpriced` |
| OpenAI | 30 s (`openai_client.py:24`) | 1 retry on timeout/network/429/5xx | 30 s ✓, same retry ✓ — move policy into `retry_call()` unchanged |
| Collector (OKX/Deribit) | 10 s | 1 retry after 0.25 s; `retry-after` honoured ≤30 s | 10 s ✓, same retry ✓ |
| SMTP | 10 s (`notifications.py:87`) | none | **15 s** — differs today (Q3) |
| keyring | none — synchronous, can block indefinitely | none | 5 s — needs a bounded executor call (Q4) |
| SQLite | `BUSY_TIMEOUT_MS` in `db.py` | none | locked-past-timeout → `db.locked` ✓ |
| Uvicorn/API | — | — | unhandled → `api.unhandled` + `error_id` |

## 6. Ctrl+C today

Uvicorn catches SIGINT/SIGTERM and runs the lifespan teardown:

- **During a cycle:** `state.agent_engine.stop()` sets `_stop` and *cancels* the
  scheduler and watcher tasks; `run_cycle` is cancelled wherever it awaits —
  mid-pair, mid-brief, mid-model-call. Sessions roll back on GC/close; a commit
  in flight can be interrupted between broker mutation and `session.commit`.
- **During a fill:** `_execute_draft` mutates the broker then commits; a
  cancel between those steps loses the fill *or* the position update — the two
  are not one transaction against cancellation.
- **During a model call:** the SDK request is cancelled; the `llm_calls` row
  may or may not exist. Treated as `model_error` on next read — acceptable.
- **During a backup:** `asyncio.to_thread` is cancelled; the worker thread
  keeps copying detached and can leave a half-written `agent-<date>.db` in
  `backups/` — later mistaken for a valid backup by `backup_due`.
- **No:** shutdown flag/`shutting_down` 503s, grace period, ordered drain,
  second-Ctrl+C semantics, "Shutdown complete" line, lock-file heartbeat, or
  unclean-start detection. The lock file survives a clean exit with its stale
  PID (flock releases, the file remains), so existence alone can't detect a
  crash — the spec's heartbeat solves that.

## 7. Where today differs from the failure catalogue

1. `context_loop` (and every ad-hoc job loop) dies permanently on one failure —
   the catalogue wants the job recorded as failed, the next run on schedule,
   and the task restarted (`task.crashed`/`task.crash_loop`).
2. `engine._watcher_loop`/`_scheduler_loop` task deaths are silent — crashes
   *inside* are caught, but a crash of the loop itself (or the task being
   dropped) stops exits/cycles with nothing visible.
3. Clock-offset sampling swallows all failures (`clock.offset_failed` missing).
4. Order-book reads for limit entries are silent (`book.unavailable` missing).
5. SMTP timeout is 10 s, spec says 15 s.
6. Keyring reads have no timeout and only `warnings.warn` (`secrets.unavailable`
   wants ERROR + Needs you key item).
7. No `error_id` anywhere; API errors return FastAPI's `{"detail": …}` shape,
   unhandled exceptions return plain text.
8. No event registry, no `X-Request-Id`, no trace context.
9. Notification failures inside the engine are swallowed (`_notify` returns
   silently); `briefing_loop` swallows per-tick failures silently.
10. Settings-history write failures are swallowed (`repo.py:85`).
11. Activity "error" rows are the only error record — no fingerprint grouping,
    no review workflow. Readiness counts unresolved `errors` rows, not
    `log_problems`.
12. The unclean-start check is missing: leftover lock file → no WARNING, no
    `PRAGMA quick_check`, running cycles not marked `interrupted`.
13. Shutdown has no graceful drain and no "Shutdown complete" line.
14. Backup can leave a half-written file that satisfies `backup_due` (partial
    copy kept as if complete) — fixed by writing to a temp name and renaming.

## 8. Questions (behaviour changes — not fixed in this PR)

- **Q1 (from the kickoff):** when the trading database can't be written, should
  the engine keep the watcher running on memory, stop and flatten, or pause?
  This PR logs CRITICAL `db.unwritable` and raises the Needs you degraded item
  without changing the engine's behaviour.
- **Q2:** `context_loop` currently dies on first failure; with `spawn()` it
  restarts forever with backoff. OK — that's the catalogue — but the collector
  currently shares one `httpx.AsyncClient`; a restart policy re-creates the
  loop, not the client. Confirm restart recreates the collector.
- **Q3:** SMTP timeout 10 s → 15 s per the spec's timeout table. Small constant
  change; flagged because it is a number.
- **Q4:** keyring calls are synchronous and can block; a 5 s timeout means a
  bounded thread (`asyncio.wait_for(to_thread(...))`). A timed-out read then
  counts as "not set" — confirm that's the intended mapping of
  `secrets.unavailable`.
- **Q5:** `_note_cycle_error` moves the agent to `error` status after 3
  consecutive cycle crashes. The catalogue doesn't list that transition; keep
  it (existing behaviour) and just add the log lines?
- **Q6:** the spec gives the stream events (`stream.disconnected`, `stream.down`)
  but no stream client exists — the watcher polls REST. I treat ticker-poll
  failure as `watcher.unpriced` per position; no stream events emitted.
