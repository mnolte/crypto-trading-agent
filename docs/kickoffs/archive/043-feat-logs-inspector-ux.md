# 043 · feat · Logs and LLM inspector UX

Kickoff for Devin. Start the session with: "Build docs/kickoffs/043-feat-logs-inspector-ux.md."

The LLM inspector gets what Stock Trading Agent's has: filters, a summary, a status with explained reasons per call, "Does it need me?", and details that explain before they show JSON. The Logs screen gets "Does it need you?" on its recurring problems. Nothing the inspector can do today is removed, and no decision, gate, prompt or stored request changes. Spec: `docs/SPEC.md` → v0.3 → LLM inspector, easier to read (new); v0.3 → Logging, errors and the Logs screen → Logs screen → Recurring problems; GUI screens → LLM inspector; the Needs you table; the API table. Design: `design/screens/Inspector.dc.html` (and `#err`), `Logs.dc.html`, their screenshots, `design/DESIGN.md` → LLM inspector screen and Logs screen. Issue #96.

```text
043 (Logs and LLM inspector UX). Goal: the LLM inspector and the Logs
screen as the spec and design now describe them. Decisions, gates, sizing,
exits, prompts and what a call stores as request and response don't change.
Start from the latest main.

Names (docs/work-items.md): branch feat/043-logs-inspector-ux, PR titles
"[043] feat: Logs and LLM inspector UX (backend)" and "[043] feat: Logs and
LLM inspector UX (screens)", commits start with [043], screenshots in
docs/screenshots/043/. Update docs/changes/043-feat-logs-inspector-ux.md in
each PR: add the PR to build_prs, status building in the first and merged in
the last, and a dated section "## YYYY-MM-DD · build PR #n".

Read first:
1. docs/changes/043-feat-logs-inspector-ux.md and
   design/changes/043-feat-logs-inspector-ux.md
2. docs/SPEC.md → v0.3 → LLM inspector, easier to read (all of it); GUI
   screens → LLM inspector; Outcome tracking → Where it shows; v0.3 →
   Logging, errors and the Logs screen → Logs screen; v0.3 → Shell and
   Dashboard → Needs you
3. design/DESIGN.md → LLM inspector screen, Logs screen; the screen files
   Inspector.dc.html (open it with #err too) and Logs.dc.html with their
   five screenshots. Texts in the design are final.
4. AGENTS.md → hard rules 2 (money as Decimal; no money arithmetic in the
   frontend), 3 (thresholds from config), 10 (no network in tests), 11 (UTC
   in logic, Amsterdam only on screen)
5. Stock Trading Agent's app/llm/reasons.py and its inspector page, for
   reference only

PRs: two. The first (backend) says "Part of #96" and moves this kickoff
unchanged to docs/kickoffs/archive/ with git mv. The second (screens) says
"Closes #96" after you have checked every point of the issue against main
plus the PR and listed them under "Issues closed".

What exists today (main on 25 November):
- LlmCallRow (backend/app/db.py:262): status (ok, invalid_output, timeout,
  api_error, input_blocked), attempts, input_checks and output_checks (JSON
  lists of {name, status, detail}), feature (decision, replay, briefing,
  ask, shadow:<id>), verdict fields. Written in deciders/openai_decider.py,
  agent/replay.py, api/inspector.py (replay) and features/briefings.py.
- backend/app/api/inspector.py: list (pair, status, issues, verdict,
  outcome, skipped, before, limit), export.jsonl, detail, download, replay,
  verdict, the scorecard. Raw payloads older than 90 days are cleared by
  POST /api/maintenance/prune.
- backend/app/api/logs.py: GET /api/logs/problems returns every problem with
  meaning and what_to_do from logevents.py, newest first.
- frontend/src/pages/Inspector.tsx (734 lines) and Logs.tsx (549 lines).

Steps, PR 1 (backend), one commit each:

1. Settings (models.py and item 025's ranges and directions maps; all
   neutral in the directions map, section "display" in meta):
   - inspector.slow_call_seconds = 15 (1–120)
   - inspector.page_size = 50 (10–100)
   - inspector.attention.act_in_a_row_cycles = 3 (2–20)
   - inspector.attention.act_429_per_day = 3 (1–100)
   - inspector.attention.act_invalid_count = 3 (1–20) and
     invalid_window = 20 (5–100)
   - inspector.attention.act_number_check_count = 3 (1–10),
     number_check_window = 5 (2–20)
   - inspector.attention.watch_within_cycles = 2 (1–20)
   - inspector.attention.watch_slow_per_day = 3 (1–100)
   - inspector.attention.watch_claim_share_pct = 20 (1–100),
     claim_window = 20 (5–100)
   - ui.log_watch_min_count = 5 (1–1000)
2. Reasons: backend/app/agent/reasons.py.
   - REASONS: one entry per code in the spec's table (changed_by_code,
     claim_mismatch, check_warning, input_warning, retried, slow,
     number_check_failed, timeout, api_error, invalid_output,
     input_blocked) with label, meaning, likely_cause, what_to_do, copied
     from the spec. api_error's cause and what-to-do are picked by status
     (401/403, 404, 429, 5xx, none).
   - classify(row, final_action, slow_s) -> (outcome_status, reasons):
     error from row.status (timeout, api_error, invalid_output,
     input_blocked); otherwise warnings: changed_by_code when the parsed
     action differs from the decision's final action (detail: the gate's
     skip_reason or the failed output check's name); claim_mismatch when
     the output check "Signal claims" is warn; check_warning for any other
     output check with warn (detail: its name); input_warning for an input
     check with warn (detail: its name and text); retried when attempts has
     more than one entry and the last answered; slow when the answering
     attempt's latency is above slow_s; number_check_failed for briefing,
     review and ask rows whose number check failed. A fail in "Rules
     baseline" is never a reason. No warning: success.
   - why_here(reason, row) -> one sentence from the row's own data (the
     check's detail text, the gate and its numbers, the attempts' times,
     the HTTP status). No model call, no new arithmetic on money.
   - attention(session, row, reasons, cfg) -> none | watch | act, by the
     spec's rules, reading earlier rows only. A registry test fails when a
     reason has no entry.
3. Migration (a new file in backend/alembic/versions): llm_calls gains
   outcome_status, reasons (JSON text) and attention; backfill every row
   with classify() and attention "none"; indexes (account_id, at, id),
   (outcome_status, at), (feature, at). Running it twice changes nothing.
4. Write at store time: each of the four places that create an LlmCallRow
   sets the three columns through one helper (reasons.stamp(row, ...)), in
   the same transaction as today. A live decision's final action is known
   when its decision row is written; stamp there, not later.
5. Brief as sections: backend/app/agent/brief_sections.py,
   sections(request_json) -> list of {title, rows: [{label, value, read,
   path}]} in the order Call, Regime, 1h, 4h, Daily, Structure, Market
   context, Liquidity, Position, Previous decision, Limits, Data age, for
   brief v1 and v2. Values are the brief's own strings and numbers,
   formatted only (thousands separators, %, the read word); a key the
   function doesn't know goes into a last section "Other", so nothing sent
   is ever hidden. A warning that names a field returns its path, so the
   screen can mark the row.
6. API (regenerate openapi.json and the frontend types):
   - GET /api/llm-calls: add period (24h, 7d, 30d, all), from, to
     (Amsterdam dates, converted to UTC in the query), status (success,
     warning, error; the old values ok, invalid_output, timeout, api_error
     keep working), source (repeatable: live, shadow, replay, text, ask),
     action, reason (code, or code:detail), model, prompt, attention,
     cycle, cursor (keyset on (at, id)), sort (new, old); return items,
     next_cursor and total. Each item adds outcome_status, reasons
     (code, label, detail), attention, the model's action and the final
     one, the outcome label and a fold_key (pair or kind, action, status,
     reasons) that the screen folds on. issues=true maps to status
     warning or error.
   - GET /api/llm-calls/summary: totals and shares, cycles without a call,
     cost_eur by source, latency median and p95 over answered calls,
     buckets (hour for 24h, day up to 30 days, week beyond; Amsterdam
     days), option counts for every filter under the other filters, and
     top_reasons: code, label, count, one example line built from the
     newest calls of that reason, the highest attention in the group.
   - GET /api/llm-calls/{id}: add outcome_status, explained reasons
     (label, meaning, why_here, what_to_do, attention, attention_why,
     links), the result sentence, the final-action sentence, sections, the
     prompt-hash match, payload_cleared, and newer_id and older_id under
     the filters passed as query parameters.
   - GET /api/llm-calls/export.csv: the columns the spec lists; euros as
     strings with 4 decimals.
   - GET /api/logs/problems: add period (default 7d) and, per problem,
     need (act, watch, none), need_why and count_in_period, by the spec's
     rule; sort by need, then count; return the three counts.
   - Needs you: one item per act group, linking to
     /inspector?attention=act&reason=<code>&period=7d; it leaves the list
     once a later call of that group succeeds.
   List and summary answer within 300 ms on a 100,000-call fixture (a slow
   test, marked as the other slow tests are).

Steps, PR 2 (screens):

7. Inspector page, per Inspector.dc.html, top to bottom. Split
   Inspector.tsx into frontend/src/pages/inspector/ (Filters, Summary,
   Scorecard, CallList, CallDetail and its parts), as Settings was split.
   - Filters in the URL, restored on load; counts on every option; Clear
     filters; "More filters · n set" below 640 px; "Show cycles without a
     call" replaces "Show skipped"; "Has issues only" is removed.
   - Summary with the bars (click sets the period), Top reasons (click
     filters), the three counts, and the empty text.
   - The scorecard folded to one line; open state kept in localStorage in
     try/catch.
   - The list: day headers, rows, folded repeats that open in place,
     dashed rows for cycles without a call, "n of total loaded" and Load
     more, the sort switch, new calls arriving live while the period
     includes now (the accent fade, never on first load). From 900 px the
     list stays in view.
   - The details: header with Newer and Older (K and J when no field has
     focus), the buttons row, the result line, What happened, the tiles
     ("not recorded" written out), verdict and outcome, Input (chips,
     system prompt fold, sections with the marked row, Raw JSON, Copy),
     Answer (the sentence, rows, checks with warn and fail first, replays)
     or Error (type, status, message, request id, error id linking to
     Logs, attempts, what Rules answered), and the 90-day text.
   - ?call= selects a call even when the filters would hide it, and says
     so ("Shown outside the current filters · Clear filters").
8. Logs page → Recurring problems, per Logs.dc.html: the four columns, the
   need chip with the count in words and Review in one column, the counts
   above, the What to do line for act and watch, the fold after three
   "nothing to do" rows, "Nothing needs you" when none do, and the phone
   stack. The period of the page's filter is sent to /api/logs/problems.
9. Links in: the Dashboard's model-calls tile → /inspector?period=24h&
   source=live; the Decision drawer's "Open model call" → ?call=; a Logs
   line with llm_call_id → ?call= (the button exists).

Tests (each rule a pass and a fail case; no network, each test its own
database, injected time; proven in CI, never run the full suites locally):
- classify: one case per reason, an error outranking a warning, two
  warnings on one call, Rules baseline disagreement giving success;
- attention: each act and watch rule at its threshold and one below, with
  the thresholds read from settings (a test changes one and sees the level
  change); a later row never changes an earlier row's level;
- migration: backfill on a fixture with every status; idempotent;
- sections: brief v1 and v2 fixtures give the design's sections; an
  unknown key lands in Other; no value differs from the stored JSON;
- API: every filter alone and two combined; the cursor walks 120 calls
  without a gap or a repeat in both sort orders; totals equal the list;
  option counts follow the other filters; top reasons order; CSV columns;
  old parameters still work; ?call outside the filters; the 300 ms budget;
- problems: an unreviewed error is act, a reviewed one none, a fixed one
  that recurs act again, a warning at 5 is watch and at 4 none, the order;
- Needs you: an act group appears and leaves after a later success;
- frontend (Vitest): filters to URL and back; a folded row opens; the
  result sentences; "not recorded"; the marked brief row; Error instead of
  Answer; K and J; the problems fold and the "Nothing needs you" line;
- e2e: inspector.spec.ts on the seeded data: open from the Dashboard tile,
  filter Status Error, open the call, see What happened and the Error
  block, Newer and Older, set a verdict; no sideways scroll at 390 px.
  Extend pr15.spec.ts for the need column. Wait for the first data as the
  other specs do now, not 5 s.

Screenshots (docs/screenshots/043/): Inspector at 1440 × 900 and 390 × 844,
a warning call and an error call opened, the summary with Top reasons, the
scorecard open; Logs → Recurring problems at both widths.

Rules:
- Don't change decisions, gates, prompts, the brief, what request_json and
  response_raw hold, verdicts, replays or the scorecard's numbers.
- Explanations come from reasons.py through the API; the frontend writes no
  explanation of its own and does no money arithmetic.
- Don't edit docs/SPEC.md, design/ or backend/prompts/ (ask in the PR);
  never edit a merged migration.
- Secrets never in responses, logs or fixtures; request headers are not
  stored and not shown.

Done when: CI is green on the last push of each PR and both merge cleanly;
every point of #96 is met and listed under "Issues closed" in PR 2; the
item's file is updated and this kickoff is in docs/kickoffs/archive/.
```

## Why

Marc asked for better Logs and LLM inspector screens, taking the stock agent as the example (25 Nov). The crypto inspector opens with the scorecard, has no period or summary, and shows a warning as a count on a row; to learn what it means Marc reads raw JSON. The stock inspector answers "which calls need a look, what happened, do I have to do anything?" first. On Logs the two screens are already close; what crypto lacks is the same "does it need me?" answer on recurring problems.

## Assumptions

- "Status" is the word for success, warning and error on this screen, because "outcome" already means a decision's result after 72 hours in this app. The stock agent calls it outcome.
- Periods are rolling hours and Amsterdam days, not sessions, because the agent trades day and night. The default is 24 hours.
- Status, reasons and attention are stored with the call, as in the stock agent, so the list and summary stay fast and a call's status never changes later. Existing rows are backfilled with attention "nothing to do".
- The scorecard keeps its own period, because its numbers need decisions that are at least 72 hours old.
- The thresholds for "needs you" and "watch" are copied from the stock agent and adapted to a retry and 15-minute cycles; they are settings.
- Logs keeps its layout. Only Recurring problems changes.

## Open questions

- A timeout is an unreviewed error on Logs (needs you) and "keep an eye on it" in the inspector until it happens 3 cycles in a row. Both follow their own rule, as in the stock agent. Should the inspector follow Logs instead?
- Should the scorecard move to Insights? It reads as a result of the model, not as a tool to debug a call.
