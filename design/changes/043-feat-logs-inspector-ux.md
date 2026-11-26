# 043 · feat · Logs and LLM inspector UX

## 2026-11-25 · spec PR

- `design/screens/Inspector.dc.html` rebuilt after Stock Trading Agent's inspector (its items 018 and 074). New: a filter card (period, status, source, and pair, action, reason, model, prompt, verdict, outcome), the Filtered calls summary (counts, cost by source, latency, a bar per hour, Top reasons with "Does it need me?"), the call list grouped per day with reason chips and folded repeats, and details that start with one sentence and a What happened panel. The brief shows as labelled sections and the answer as rows, each with Raw JSON one click away.
- Kept from the old screen, in new places: the scorecard (folded to one line under the summary), Re-run, Open in Indicators, Download JSON, the verdict buttons and note, the outcome line, the input chips, the output checks (warn first), replays, and the row for a cycle without a call.
- The scenario follows Logs and Universe (Tue 13 Oct 12:34): 41 calls in 24 hours (38 live decisions, 1 briefing, 2 Ask), 33 success, 6 warnings, 2 errors; the two errors are the BTC-EUR and ETH-EUR timeouts that Logs shows (call 5531, error id e-3f9a21c0). `Inspector.dc.html#err` shows that timeout.
- `design/screens/Logs.dc.html` → Recurring problems: a "Does it need you?" column (needs you, watch, nothing to do) with the count in words and Review, the three counts above the table, sorted by what needs Marc, a "What to do" line for the two that matter, and the rest folded ("4 more with nothing to do"). The Count and Review columns merged into it.
- Screenshots: `Inspector.png`, `Inspector-390.png`, `Logs.png`, `Logs-390.png` re-rendered; `Inspector-error.png` new. `Logs-trace.png` is unchanged.
- `design/DESIGN.md`: the Inspector row, a new "LLM inspector screen" section, the Logs screen rules, and a row in "Which PR builds which screens".

## 2026-11-25 · second spec PR (Marc's two answers)

- **The inspector follows Logs.** The timeout is now "Needs you" in Top reasons (listed first; the counts read 2 need you · 0 to watch · 6 nothing to do) and in the error call's What happened panel, which gains a "Review…" button and a new What to do text.
- **The scorecard moved to Insights:** removed from `Inspector.dc.html`; `Insights.dc.html` has it as a full-width card at the bottom. The inspector's outcome block links to it.
- Screenshots re-rendered: `Inspector.png`, `Inspector-390.png`, `Inspector-error.png`, `Insights.png`, `Insights-390.png`. `design/DESIGN.md` updated.
