# 016 · feat · Positions & orders

Kickoff for Devin. Start the session with: "Build docs/kickoffs/016-feat-positions-orders.md."

```text
016 (Positions & orders, build plan item 16). Goal: one screen that shows
everything the paper broker holds and did, so any fill or exit can be
checked against its prices: open positions with their stop history and
distances, working limit orders, today's entries and exits, the paper
account, and every fill with its reference price, slippage and fee. The
screen only reads; it never changes a position. Fill prices, fees,
slippage, exits and the cycle don't change: this item records and shows
them. Start from the latest main, with 015 (#22), 023 (#34) and this
kickoff's PR merged.

Names (docs/work-items.md): branch feat/016-positions-orders, PR title
"[016] feat: Positions & orders", commits start with [016], screenshots in
docs/screenshots/pr-16/. When you open and merge the PR, update row 016 in
docs/work-items.md.

Read:
1. design/CHANGES.md, entry 2026-11-04 "[016] Positions & orders design";
   docs/CHANGES.md, the entries since 015
2. docs/SPEC.md → v0.3 → Positions & orders (the whole subsection); v0.3 →
   Shell and Dashboard (header Today, the Open positions card); Market data
   and paper broker (paper broker: fees, slippage); Risk gates (exits:
   stop, breakeven, trailing, time stop, kill switch); v0.2 → Limit
   entries, tested as Shadow B (paper_orders, unfilled); User features →
   Shadow strategies; Settings, secrets and persistence (Reset paper
   account)
3. design/DESIGN.md → Positions & orders screen, Screens and The shell;
   design/screens/Positions.dc.html with Positions.png, Positions-390.png
   and Positions-shadow.png (texts are final); States.dc.html (card states)
4. AGENTS.md → hard rules (Decimal for money, no real network calls in
   tests, never delete data/)

What exists today (main on 4 November):
- Tables: positions (pair, account_id, qty, entry, stop, initial_stop,
  target, opened_at, decision_id, last, highest_close, r_unit,
  entry_maker), trades (fee_eur for both sides, realised_eur, realised_r,
  exit_reason, decision_id), paper_orders, stop_moves (old_stop,
  new_stop, reason breakeven or trailing, at), accounts, shadows (each
  with its own account_id).
- No record of single fills. PaperBroker.buy() and close()
  (agent/broker/paper.py) know the reference price, fill price, order
  type and fee at the moment of the fill, but store only the position's
  entry and the trade's exit.
- Exit reasons in code: stop, target, time_stop, model_sell, flatten,
  manual, kill. Screen labels: stop, target, time stop, model exit,
  Stop & flatten, closed by you, kill switch. Every entry is "decision"
  (also when approved in supervised mode).
- No GET /api/positions: positions reach the frontend in the snapshot.
  The stop distance, ATR distance and P&L in R are computed inline in
  api/deps.py; open risk is PaperBroker.open_risk().
- The exit watcher's last pass time is not kept anywhere.

Build 016 only, in this order (one commit per step):

1. Fills. An Alembic revision adds `fills`: id, account_id, at, pair,
   side (buy, sell), qty, reference_price (null for a rebuilt fill),
   fill_price, order_type (market, limit), fee_eur, fee_kind (maker,
   taker), slippage_eur (null for a rebuilt fill), reason (decision,
   stop, target, time_stop, model_sell, flatten, manual, kill),
   decision_id, trade_id (set on the exit, and on the entry once the
   trade closes), paper_order_id, source (live, rebuilt). Indexes on
   (account_id, at) and (account_id, pair, at).
   - PaperBroker.buy() and close() write one row in the same transaction
     as the position or trade change: both stored or neither.
     reference_price is the price passed in (the last trade for a market
     order, the watcher's price that crossed a stop, the limit for a
     limit fill); slippage_eur = |fill − reference| × qty, 0 for a limit
     fill.
   - Shadows' brokers write too, with their own account_id. What-if,
     replays and anything else that simulates on a throwaway account
     never leave fills behind.
   - Rebuild once at the first start after the revision: one entry fill
     per trade and per open position, one exit fill per trade, source
     "rebuilt", reference_price and slippage_eur null. Split a trade's
     fee_eur over entry and exit in proportion to notional × fee rate,
     with the maker rate where a filled paper_orders row exists for that
     entry or target, else taker. Record that the rebuild ran, so it
     runs once. Don't change trades or positions.
2. Shared position facts. Move the per-position maths out of api/deps.py
   into one function used by the snapshot and the new endpoint: distance
   to the stop (% and ATR 1h) and to the target, P&L in € and R, result
   if the stop hits (€, R and % of equity, before fees; positive when the
   stop is at or above entry), R/R at entry, time stop time
   (opened_at + risk.time_stop_days), and the stop near flag
   (ui.stop_near_atr). Open risk stays PaperBroker.open_risk(). The
   Dashboard must show the same numbers as before.
3. Watcher check time. The engine keeps the time of the last completed
   watcher pass per account (live and each shadow), in memory. After a
   restart it is null until the first pass; the screen then shows
   "Exit watcher not checked since the restart".
4. API.
   - GET /api/positions?account=live|<shadow id>: totals (count,
     unrealised, if every stop hits, open risk % and its cap, exposure €
     and %, cash, fees today and since the account start, the position
     closest to its stop in ATR), per position the facts from step 2
     plus stop_history (the initial stop at opened_at, then each
     stop_moves row in order), latest_decision (id, at, action,
     confidence, limit price if any) and watcher_checked_at; and
     working orders: open paper_orders rows of that account with limit,
     qty, placed_at, expires_at, last price and min_price as "low since
     placed". An unknown shadow id is a 404 in the error shape from 015.
   - Only for account=live, two more blocks: today (entry and exit fills
     since 00:00 Amsterdam, exits with the trade's realised_eur,
     realised_r and its fees; counts, realised total and fees today) and
     account (number, started_at, start balance, cash, coins value,
     equity, realised P&L and closed trade count of the current account,
     fees and slippage in total from fills).
   - GET /api/fills?pair=&side=&reason=&from=&to=&cursor=: live account,
     newest first, 50 per page with an opaque cursor; each row with
     decision_id and trade_id. reason takes the code values from step 1.
   - GET /api/fills/export.csv with the same filters and every matching
     row: at_utc, at_amsterdam, pair, side, qty, reference_price,
     fill_price, order_type, slippage_eur, fee_eur, fee_kind, reason,
     decision_id, trade_id, source. Plain decimals with a dot, no
     currency signs, file name fills-YYYY-MM-DD.csv.
   - Regenerate openapi.json and the frontend types.
5. Frontend. Route /positions; "Positions & orders" in the sidebar under
   Monitor right after Dashboard; the Dashboard's "Open positions" title
   links to it. The screen per Positions.dc.html at both widths:
   - the Live / Shadow A / Shadow B switch lists the shadows that exist
     (running or stopped, by name) and changes the totals, the position
     cards and Working orders; Today, Paper account and Order history stay
     live and say so;
   - "Latest decision", "decision …" and "trade #n" open the Decision
     drawer (for a trade: its entry decision);
   - positions refresh on every snapshot over the WebSocket; the order
     history reloads when a new fill arrives;
   - order history filters keep their values in the URL; Export CSV
     downloads with the current filters;
   - card states from States.dc.html. Empty texts (use these exactly):
     no open positions: "No open positions. The next cycle is at
     {time}."; a shadow with none: "Shadow {name} holds nothing right
     now."; no fills for the filters: "No fills match these filters.";
     working orders for live with market entries: the sentence in the
     design; with limit entries and none waiting: "None waiting."
6. Tests (no network; fakes, the simulated market and a fake clock):
   - a market buy, a limit entry, a stop, a target (market and limit),
     a time stop, a model exit, Stop & flatten, a close by you and a kill
     each write one fill with the right reason, prices, fee kind and
     slippage; a failure between the trade and the fill leaves neither;
   - shadow fills carry the shadow's account; a What-if run leaves no
     fills;
   - the rebuild: rows for every trade and open position, fees per trade
     add up to fee_eur within €0.01, a second start adds nothing;
   - step 2: the snapshot's numbers are unchanged (compare before and
     after on the same fixture); stop above entry gives a positive result
     if the stop hits;
   - /api/positions for live and a shadow; an unknown shadow is a 404;
     working orders come from open paper_orders only;
   - /api/fills: each filter, the cursor across pages, newest first;
     the CSV header, a row, and that filters apply;
   - today's block uses 00:00 Amsterdam, including across the switch to
     winter time;
   - frontend: the switch, the filters in the URL, each empty text, the
     Decision drawer links;
   - Playwright: Positions at 1440 × 900 and 390 × 844, and the Shadow B
     view, in docs/screenshots/pr-16/; no sideways scroll at 390 px.

Do not:
- change how fills are priced, fees, slippage, exits, sizing, gates or
  the cycle; this item only records and shows them;
- add anything that changes a position from this screen (closing a
  position stays where it is now);
- write fills from What-if, replays or backtests;
- edit docs/SPEC.md, design/ or backend/prompts/ (ask instead);
- delete data/, a database or a backup outside a test's own folder;
- put screenshots in design/.

In the PR description: the screenshots next to design/screenshots/
Positions.png, Positions-390.png and Positions-shadow.png; "Design
differences" if any; how many fills the rebuild made on your dev database
and how long it took; and open questions, at least:
- any trade whose fee_eur couldn't be split cleanly in the rebuild;
- anything in the code that disagrees with the spec section.
```
