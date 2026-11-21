# Design changes

Newest first. Each entry names the screens it touches.

## 2026-11-04 — [016] Positions & orders design

- New `Positions.dc.html` for build plan item 016, in Stock Trading Agent's layout and with the crypto spec's content (v0.3 → Positions & orders). Stock parts that don't apply to a paper broker are left out: reconciliation with the broker, bracket legs, sessions and the day-trade note. The scenario is the Dashboard's at Tue 13 Oct 12:34: BTC, SOL and ETH open, XRP sold at 12:00, SOL's stop at breakeven since 03:15.
  - Live / Shadow A / Shadow B switch; totals line (unrealised, if every stop hits, open risk, exposure, cash, fees today and since start, closest to its stop);
  - a card per position: the stop-to-target bar, If the stop hits, To the stop, To the target, Time stop, Stop history, the exit watcher's last check and the latest decision;
  - Working orders (empty for live with the reason; Shadow B has a LINK-EUR limit entry, `Positions-shadow.png`);
  - Today (since 00:00 Amsterdam), Paper account, and Order history with pair, side, reason and period filters, Export CSV, reference price, fill, slippage and fee per fill.
- "Positions & orders" in every sidebar under Monitor after Dashboard; the Dashboard's Open positions title links to it.
- Screenshots: `Positions.png`, `Positions-390.png`, `Positions-shadow.png`, and every screen with a sidebar again.

## 2026-11-04 — [023] Logs in every sidebar

- **Logs in the sidebar** of every screen with one (Dashboard, Indicators, Performance, Insights, What-if, Reviews, LLM inspector, Ask, Readiness, Settings), under Control after Readiness, as in Stock Trading Agent. Until now only the Logs screen itself had the item, so the design had no way to reach it. Decision, States and Errors have no sidebar.
- **Readiness:** the errors criterion follows v0.3 → Logging, errors and the Logs screen: "Errors in the log", no unreviewed error or critical problem and no critical line in the last 7 days, with "review on Logs" next to its status (as in Stock Trading Agent). It replaces "No unresolved errors … resolve on Dashboard".
- The Logs screen itself is unchanged.
- Screenshots updated: the ten screens above, at 1440 and 390 px.

## 2026-11-04 — Logs screen and error pages

- New `Logs.dc.html`, adapted from Stock Trading Agent's Logs screen to crypto (pairs instead of symbols, UTC on hover, the crypto components, the same scenario as the Dashboard: the two OpenAI timeouts at 09:15 and 09:30, the sleep 23:15–23:45):
  - filters: level toggles with counts, "Show debug and info", period, search, component, pair, "Cycle, error or request id", Clear filters, Download .jsonl; at 390 px the text filters sit behind "More filters";
  - warnings and errors per day for 7 days;
  - recurring problems with unreviewed, expected (with its note) and fixed, and Review;
  - the log per day; the 09:31 error is opened with **What this means** and **What to do** from the event registry, the chained traceback, the stored line, and the surrounding lines of cycle c-0716;
  - **trace view**: "Every line of cycle c-0716" shows all levels of that cycle with a banner (`Logs-trace.png`);
  - footer: lines loaded, the size of `logs.db` and the retention per level.
- Changes from the stock design: event meaning and what to do in the opened line; Copy error id; the trace view; error and request ids as a filter; at phone width the message runs full width under the time and level, and tracebacks scroll sideways.
- Sidebar: a Logs item under Control after Readiness (drawn in `Logs.dc.html`; every screen's sidebar gets it in PR 15).
- Screenshots `Logs.png`, `Logs-390.png`, `Logs-trace.png`.
- New `Errors.dc.html`, from Stock Trading Agent's error pages: in the app, not found (a record, a page), something went wrong (Reload page, Copy error id, Open in Logs) and the app isn't answering (Try now); served by the reverse proxy, app down (502/504) and updating (503). Changes for crypto: no broker holds the stops, so the pages say that positions are paper only and that stops and targets are checked only while the app runs, against the current price as soon as it is back; updates wait until no cycle is running and the app starts paused; local addresses for the in-app pages; at 390 px the mock sidebar is hidden as in the app. Screenshots `Errors.png`, `Errors-390.png`.

## 2026-11-04 — Cycles for a 24/7 market

- `Main.dc.html`: the cycle strip becomes the **Cycles** card. Nothing counts per day any more ("Cycle n of 96 today" and "this hour's four cycles" are gone):
  - top: the quiet line; next cycle with countdown, a bar through the current 15-minute interval, "every 15 min, day and night" and Run now;
  - a rolling timeline of the last 24 hours and the next 3: one slot per quarter hour, coloured by what happened (no change, entry B, exit S green or red, stop moved ↑, error answered by Rules !), missed slots as an outline, the next slot in amber, scheduled jobs as purple diamonds under their slot, time labels every 3 hours and the date at midnight;
  - a summary: 93 of 96 cycles ran in 24 h, 3 missed 23:15–23:45 (computer asleep), 2 errors, cycle #728, running 7 d 14 h;
  - at 390 px the timeline shows the last 6 hours and the next 3.
- "Today's decisions" becomes **Decisions · last 24 hours**: the oldest group reads "Mon 12:45–11:45 · 90 cycles · 2 changes · 3 missed".
- Model calls today tile: 153 calls (held positions are reviewed every cycle), €0.06.
- Screenshots `Main.png` and `Main-390.png` re-rendered.

## 2026-11-03 — Aligned with Stock Trading Agent

Stock Trading Agent's design now leads; this entry brings Trading Agent in line with it. Behaviour does not change, except where noted. Build it as one UI PR after the current work, in this order.

1. **Tokens** (`tokens.css`): the same structure and names as Stock Trading Agent, with the `--ta-` prefix, plus the interaction classes (`.ta-row`, `.ta-tile`, `.ta-skel`, focus ring, phone breakpoint). Colours are unchanged except the chart series. Rename map:

   | Old | New |
   | --- | --- |
   | `--bg`, `--bg-sunken`, `--panel`, `--panel-raised`, `--panel-hover` | `--ta-bg`, `--ta-chrome`, `--ta-panel`, `--ta-raised`, `--ta-subtle` |
   | `--line`, `--line-strong` | `--ta-border`, `--ta-border-strong` |
   | `--text`, `--text-soft`, `--text-muted` | `--ta-text`, `--ta-text-2`, `--ta-text-muted` |
   | `--text-group` (#8A94A1), `--text-faint` (#6B7480) | `--ta-text-faint`, `--ta-text-disabled` |
   | `--accent`, `--accent-ink`, `--accent-soft`, `--accent-bg`, `--accent-line` | `--ta-accent`, `--ta-accent-ink`, `--ta-accent-hover`, `--ta-accent-bg`, `--ta-accent-border` |
   | `--up*`, `--down*` | `--ta-positive*`, `--ta-negative*` (`--down-text` → `--ta-negative-text`, `--down-soft` → `--ta-negative-soft`) |
   | `--warn-*`, `--info-*`, `--side*` | `--ta-warning-*`, `--ta-info*` (`--info-icon` → `--ta-info`), `--ta-neutral-chip*` |
   | `--series-*` | `--ta-chart-*` |
   | `--font-ui`, `--font-num` | `--ta-font-sans`, `--ta-font-mono` |
   | `--fs-caption` … `--fs-page` | `--ta-text-sm` … `--ta-text-xl` (new `--ta-text-xs` 11 px) |
   | `--fs-kpi` 24 px, `--fs-price` 22 px | `--ta-text-stat` 22 px, `--ta-text-price` 24 px (swapped, as in Stock Trading Agent) |
   | `--radius-sm`, `--radius`, `--radius-card`, `--radius-dialog` | `--ta-radius-sm`, `--ta-radius-md`, `--ta-radius-lg`, `--ta-radius-xl` |
   | `--hit`, `--sidebar`, `--sidebar-collapsed`, `--drawer`, `--ask-panel` | `--ta-control-height`, `--ta-sidebar-width`, `--ta-sidebar-collapsed`, `--ta-drawer-width`, `--ta-ask-panel` |
   | `--topbar` (76 px) | removed: the header grows with its content |

2. **Chart series**: EMA 20 blue (#7FB8FF), EMA 50 purple (#C792EA), VWAP amber dashed (#F2B94B), as in Stock Trading Agent. Dashboard and Indicators.
3. **Shell, every screen** (`DESIGN.md` → The shell):
   - the header carries the sub line, PAPER badge, status pill with the next cycle, Needs you chip, Equity, Today, Regime (BTC), Ask, Pause and Stop & flatten;
   - Ask the agent moves from the sidebar to the header;
   - the Paper/Live switch becomes the PAPER badge (paper only); Run now moves to the cycle strip; News flag moves to the risk budget; the "2 errors · 24h" chip becomes a Needs you item;
   - screen controls move into the page: Performance period and export, Insights filters (a filter card), Settings Save and Discard (a sticky unsaved-changes bar).
4. **Phone width**: below 820 px a sticky top bar replaces the sidebar. Every screen with the shell works at 390 px; wide tables scroll inside their card. `-390.png` screenshots added.
5. **Dashboard** (`Main.dc.html`) in the Stock Trading Agent order:
   - Needs you card with the health line (Bitvavo, price stream, clock, OpenAI key, email, collector); which items it lists is in the spec, v0.3 → Shell and Dashboard;
   - this-cycle strip with the cycle counter and Run now (made 24/7 the next day, see the entry above);
   - two tiles: model calls today, since start against holding BTC. Equity and Today moved to the header; Exposure and Drawdown to the risk budget; Next cycle to the strip;
   - open positions as rows with a stop-to-target bar and the distance to the stop in % and ATR (amber under 1.5 ATR), next to the risk budget;
   - price chart with 1h candles, EMA 20/50, VWAP, entry, stop and target lines, held-pair buttons and Find pair (pair tabs and More removed), next to the latest decision ("Open full decision");
   - today's decisions grouped by cycle (changes always visible) next to the morning briefing with its footer;
   - Markets: every pair with price, 24h, regime, rules result and last action; filters All, Pass rules, Held, Paused. Click a row to chart it.
6. **States**: card states (loading, empty, error, backend unreachable) and toasts (critical, info and warning).

## 2026-10-22 — Prompt versions in captions

- The sentiment prompt is now decision.v3 (spec: "Prompt v2: decision quality").
  Decision drawer positioning caption, States stale-source note and the Shadow A row
  say decision.v3; the Shadow B row uses v2. Wording only.

## 2026-10-21 — v0.2: limit entries (Shadow B)

- Settings → Market data & paper account: fields in a 2 × 2 grid (starting balance,
  slippage on market orders, taker fee, maker fee); new "Entry orders" segmented
  control (Market / Limit post-only) with a note on minimum stops and Shadow B.
- Settings → Shadow strategies: Shadow B row (decision v1, limit entries, "7/10
  filled", tooltip with the missed-fill average); "Add shadow" disabled at 2 of 2;
  Promote text mentions the entry order type.
- States: Decision drawer footer for a limit entry in three states: waiting for
  fill, filled (with fee and size), unfilled (cancelled, tracked as a missed fill).

## 2026-10-14 — v0.2: positioning data and event flags

All additions carry a blue v0.2 tag in the screens (handoff only, not shown in the app).

- Decision drawer: "Market positioning · BTC" section below the signal stack (funding,
  open interest, long/short accounts, DVOL, 25-delta skew; value, 90-day percentile,
  read with extremes in amber); safety gates gain News flag and Macro event rows.
- Dashboard: News flag button between Pause and Stop & flatten; when on, an amber banner
  with Clear flag, and the "This cycle" line adds "News flag on since 04:20." before
  per-coin pauses.
- Settings: Data sources table now has "OKX market data" and "Deribit options data" (on,
  with days stored); CoinGecko and alternative.me read "Off · after v0.2". Risk limits
  gain an "Event flags" block: news flag and macro event gate (off by default, with a
  link to What-if).
- States: news flag banner, macro event banner, "Set the news flag?" dialog with an
  optional reason, and a stale-source row.
- Insights: a "Positioning · v0.2" slice tab (BUY outcomes by funding and skew read at
  entry).
- What-if: macro event gate option in the scenario.

## 2026-10-11 — ADX wording

- Indicators: ADX pane label and the reads footnote say "25 or more = trending"
  (inclusive, ≥ 25), matching the spec.

## 2026-10-10 — Spec review fixes

- Settings: the cycle-interval selector is gone (cycles are fixed at 15 minutes in v0.1);
  max total exposure is 100%; max stop distance is 5 × ATR(1h); the eligibility note
  asks for 300+ days of daily history.
- Dashboard: exposure KPI shows "cap 100% · spot, no leverage"; sample BTC trade now
  uses the full 1% risk (0.0807 BTC), so exposure, open risk, positions and activity
  changed with it; SOL's stop sits at breakeven (139.10) as the briefing says.
- Decision drawer and States: same BTC numbers; exposure gate reads "≤ 100%".

## 2026-10-10 — First handoff

All 12 screens: Dashboard, Decision detail, Performance, Settings, States and dialogs,
Indicators, LLM inspector, Insights, What-if, Ask the agent, Reviews, Readiness.

Includes the regime chips and band, the portfolio safety rules (correlated positions,
shock brake, risk throttle), the invalidation price in the decision drawer, the monthly
cost cap, the approval queue and the Ask side panel.
