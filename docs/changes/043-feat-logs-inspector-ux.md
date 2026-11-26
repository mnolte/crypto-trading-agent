---
item: 043
type: feat
title: Logs and LLM inspector UX
status: ready
issue: 96
kickoff: 043-feat-logs-inspector-ux.md
spec_prs: [97, 99]
build_prs: []
---

# 043 · feat · Logs and LLM inspector UX

## 2026-11-25 · spec PR

Marc, 25 Nov: "Improve the UX and UI of the logs and LLM inspector pages. Get your inspiration from the stock trading agent." New `docs/SPEC.md` → v0.3 → LLM inspector, easier to read, and an addition to the Logs screen. No decision, gate, prompt or stored request changes.

- **A status per call:** success, warning or error, with reasons (`changed_by_code`, `claim_mismatch`, `check_warning`, `input_warning`, `retried`, `slow`, `number_check_failed`, `timeout`, `api_error`, `invalid_output`, `input_blocked`), set when the call is stored. Each reason has one fixed explanation in code: what it means, the likely cause, what to do.
- **Does it need me?** Needs you, keep an eye on it, or nothing to do, decided by code from settings under `inspector.attention`, stored as `attention`. Groups that need Marc become Needs you items.
- **The screen:** filters in the URL (period, status, source and more), a summary with Top reasons, the scorecard folded to one line, the list grouped per day with folded repeats, and details that explain first: What happened, call data, verdict and outcome, the brief as sections, the answer as rows, the checks, the Error block.
- **Storage and API:** `llm_calls` gains `outcome_status`, `reasons` and `attention` with a backfill; `GET /api/llm-calls` gets the filters, a keyset cursor and the total; new `/summary` and `/export.csv`; the detail adds the explanations and the brief as sections.
- **Logs → Recurring problems:** "Does it need you?" per problem (an unreviewed error or a critical: needs you; a warning from `ui.log_watch_min_count`, 5, times: watch), sorted by it, with the counts above, a What to do line, and the rest folded.
- Kept: Re-run, verdicts, the labelled export, Open in Indicators, Download JSON, the checks, attempts, replays, skipped cycles, the scorecard. "Has issues only" is replaced by Status and Reason.

## 2026-11-25 · second spec PR (Marc's two answers)

Marc answered the kickoff's two open questions with yes, after the first spec PR (#97) was merged and the backend build PR (#98) was opened.

- **The inspector follows Logs.** A call whose failure is an ERROR line on Logs (`timeout`, `api_error`) needs Marc from the first time, until its recurring problem is reviewed; reviewing clears it on both screens. The count-based rules for timeouts, 5xx, 401/403/404 and 429 are dropped, with their settings; the other rules stay. `llm_calls` also gets `problem_key`. These calls are covered by the Needs you item "Errors not reviewed" and get no second item.
- **The scorecard moves to Insights,** unchanged in content, following that screen's period filter.
- The merged kickoff is not edited; Devin gets these two changes as a prompt from Marc.
