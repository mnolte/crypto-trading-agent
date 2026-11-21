# Trading Agent — design

How Trading Agent looks and what it says. It uses the same visual language as Stock Trading Agent, and that app's design leads: dark "instrument panel" surfaces, IBM Plex Sans for text and IBM Plex Mono for every number, the same shell, components and interaction rules. Where the two apps differ, the difference comes from behaviour (crypto runs 24/7 on Bitvavo in euros; stocks run in sessions on Alpaca in dollars), not from taste.

## What decides what

- **The build spec decides behaviour.** If a screen shows behaviour the spec doesn't describe, the spec wins; ask in the PR.
- **This folder decides look and wording.** Copy text as written: labels, button names, banner sentences, empty states.
- Deliberate differences go under "Design differences" in the PR description.

## What's here

| Path | Contents |
| --- | --- |
| `tokens.css` | Every colour, font, size, spacing and radius, in the same structure as Stock Trading Agent with the `--ta-` prefix. Copy it into `frontend/src/styles/`; use only these variables. |
| `screens/` | The 14 screens as HTML: the exact source for styles, layout and wording. |
| `screenshots/` | One PNG per screen at 1440 px wide (Decision at 560 px), plus `<Screen>-390.png` at phone width for every screen with the shell. |
| `changes/` | Design notes per work item from 024 (`NNN-type-slug.md`); `CHANGES.md` holds the log up to item 023. |

## How to read the screen files

- Each `.dc.html` file is plain HTML with inline styles, plus a small `renderVals()` script at the bottom.
- `{{name}}` marks a value filled in by that script; `<sc-for>` repeats an element over a list; `<sc-if>` shows it conditionally. In React these become props, `.map()` and conditional rendering.
- The script holds **sample data only** (prices, trades, times). The real numbers come from the backend. Use the sample data for fixtures and Playwright tests.
- The files load a design-tool runtime (`support.js`) that is not included; read them as source and compare with the screenshots. When a screenshot and the HTML seem to disagree, the HTML is right. Don't copy the HTML into the app: rebuild it in React with the tokens.
- Blue **v0.2** tags mark what v0.2 adds. They are for the handoff only: do not render them in the app.

## Screens

| File | Screen | Spec section |
| --- | --- | --- |
| `Main.dc.html` | Dashboard, in the Stock Trading Agent layout: Needs you card with the health line; cycles card: the quiet line, next cycle with countdown and Run now, and a rolling 24-hour timeline of every cycle (state per slot, missed slots, scheduled jobs); two tiles (model calls today, since start); open positions with a stop-to-target bar next to the risk budget (open risk, exposure, today's loss, drawdown, room, correlation, throttle, shock brake, cooldown, news flag); price chart (1h candles, EMA 20/50, VWAP, entry, stop and target lines, held-pair buttons, Find pair, 1h/4h/1d) next to the latest decision with per-coin pause; the decisions of the last 24 hours grouped by cycle next to the morning briefing; Markets table last | GUI screens → Dashboard; User features (quiet line, briefing, risk budget, per-coin pause) |
| `Decision.dc.html` | Decision detail drawer, 560 px | GUI screens → Decision detail |
| `Performance.dc.html` | Performance | GUI screens → Performance |
| `Settings.dc.html` | Settings, with a sticky unsaved-changes bar | GUI screens → Settings; Settings, secrets and persistence |
| `States.dc.html` | Banners, dialogs, approval queue, notification, card states (loading, empty, error, backend unreachable) and toasts | States |
| `Indicators.dc.html` | Indicators | GUI screens → Indicators |
| `Inspector.dc.html` | LLM inspector | GUI screens → LLM inspector; Outcome tracking |
| `Insights.dc.html` | Insights, filters in a card under the header | User features → Insights |
| `WhatIf.dc.html` | What-if | User features → What-if simulator |
| `Ask.dc.html` | Ask the agent panel, 520 px over the dimmed screen | User features → Ask the agent |
| `Reviews.dc.html` | Reviews | User features → Weekly review, Daily briefing |
| `Readiness.dc.html` | Readiness | User features → Live-readiness checklist |
| `Errors.dc.html` | Error pages, a catalogue like States: in the app (not found for a record and a page, something went wrong, the app isn't answering) and the static pages for the reverse proxy (app down, updating). The texts are final | v0.3 → Logging, errors and the Logs screen → Error pages and API errors |
| `Logs.dc.html` | Logs: filters, warnings and errors per day, recurring problems with review state, the log grouped per day with one error opened (meaning, what to do, exception, stored line, surrounding lines); clicking "Every line of cycle c-0716" shows the trace view (`Logs-trace.png`) | v0.3 → Logging, errors and the Logs screen |

The States board is a catalogue, not a page: each banner, dialog and card on it belongs to the screen named in its label.

## The shell

Every screen except the Decision drawer has the same shell. Build it once.

- **Sidebar**, 232 px, collapsing to a 72 px icon rail (choice remembered per browser): app mark and name, groups Monitor / Learn / Control, the status at the bottom, Collapse. Ask the agent is no longer in the sidebar; it is in the header.
- **Header** on every screen, wrapping on narrow widths:
  - left: the screen title with one sub line under it (date and time on the Dashboard, what the screen covers elsewhere), the PAPER badge, the status pill ("Running · next cycle 12:45 · in 11 min"; Paused and Killed variants), and the Needs you chip;
  - right: Equity, Today, Regime (BTC), then Ask (accent outline, Ctrl/⌘+J), Pause, Stop & flatten.
  - Screen-specific controls (filters, period, export, save) sit at the top of the page, not in the header. Settings saves through the sticky unsaved-changes bar.
- **Needs you**: a chip in the header ("Needs you · 2") and a card first on the Dashboard. Items are things only the user can resolve (errors not reviewed, verdicts to confirm, a failing key or data source), most urgent first, each with one action. Items leave when they're done. With none, the chip is hidden and the card collapses to the health line.
- **Phone width**: below 820 px the sidebar is replaced by a sticky 56 px top bar with the app mark, the screen title, the Needs you count, Ask and a menu button that opens the sidebar as an overlay. The header's Needs you chip is hidden there.

## Shared components

Build these first, in PR 1, on a hidden `/dev/components` page that shows every component in every state with sample data:

1. Sidebar with groups, active item and collapse (232 px / 72 px); phone top bar
2. Header: title and sub line, PAPER badge, status pill, Needs you chip, Equity, Today, Regime, Ask, Pause, Stop & flatten
3. Card with uppercase 13 px label and optional right-side link
4. Stat tile (label, 22 px mono value, sub line, optional bar; linked tiles get "Open →")
5. Meter (value against a limit), position bar (stop, entry, last price, target)
6. Table and row list with mono numeric columns and horizontal scroll inside the card on narrow screens
7. Chip (neutral, positive, negative, warning, info, accent, sideways)
8. Action badge, small and large: BUY, SELL, HOLD, SKIP, NO CALL, STOP UP
9. Check row (pass, fail) for rules and risk gates
10. Banner (negative, warning, info) with actions; toast (critical, info and warning)
11. Dialog with confirm and cancel; typed-confirmation variant
12. Drawer (Decision, 560 px) and side panel (Ask, 520 px)
13. Buttons: default, primary, danger, ghost; segmented control; toggle switch
14. Form field with unit and help text
15. Price chart: 1h candles, EMA 20, EMA 50, VWAP, entry, stop and target lines, day separators (TradingView Lightweight Charts)
16. Equity line chart with the BTC benchmark line
17. Glossary term (dotted underline and tooltip)

## Rules

- **Numbers:** IBM Plex Mono, right-aligned in tables. Money in euros. Prices with the pair's own precision.
- **Times:** Amsterdam time, with UTC in a tooltip. Never compute the offset by hand.
- **Colour means something:** green is a gain or a pass, red a loss, a stop or a failure, amber a warning or a skip, blue a stop raised or an info note. Never use colour alone; there is always a word, a sign or an icon.
- **Chart series:** EMA 20 blue, EMA 50 purple, VWAP amber dashed, EMA 200 light grey; the same as Stock Trading Agent.
- **Controls:** buttons, nav items and clickable rows are at least 44 px high.
- **PAPER** shows in the header on every screen, always.
- **Card headings:** 13 px, weight 600, uppercase, `--ta-text-2`, letter-spacing 0.06em.
- **Sample data:** every number in the designs is sample data. Labels that say "Sample data" in the designs are for the designs only.

## Interaction rules

These apply to every screen. Inline styles can't express hover or focus, so the shared classes live once in the app's global CSS (see the end of `tokens.css`).

- **Focus:** every interactive element shows `:focus-visible` (2 px accent ring, 2 px offset). Never set `outline: none` without this replacement.
- **Hover:** clickable rows use `.ta-row`, linked tiles and cards `.ta-tile`, buttons brighten slightly. Anything clickable gives hover feedback; nothing that isn't clickable does.
- **Size:** buttons, nav items, clickable rows and feed rows are at least 44 px high (`--ta-control-height`), at phone width too.
- **Card states:** every card that fetches data has three states besides showing data: *loading* (skeleton, `aria-busy`, never the empty text), *empty* (only after a successful load; says why and what comes next) and *error* (what failed and when, a Retry, and the last data kept visible and marked stale). When two or more cards fail or the API doesn't answer, a page banner says so and retries every 15 s. See `States.dc.html` → Card states.
- **Live regions:** only changes of state are announced. Countdowns and clocks are never inside `role="status"` or `aria-live`. Critical banners and toasts use `role="alert"`.
- **Toasts:** one per notification event (not per channel), stacked below the header, newest on top, at most 3. Info and warning close after 10 s (hover pauses); critical toasts stay until dismissed.
- **ARIA patterns:** use a pattern completely or not at all. Filters are toggle buttons (`aria-pressed`), not tabs. Real tabs (that switch panels, like Reviews) support arrow keys. Lists of clickable rows are buttons in a list, not `role="row"` on a button.
- **Dialogs and drawers:** focus moves in, stays in, Escape closes, and focus returns to what opened it. The phone sidebar overlay follows the same rule.
- **Numbers:** the frontend formats numbers from numeric API values, so every negative uses the minus sign (−, U+2212). The backend never sends formatted strings.
- **Copy that names a setting** (for example "6-hour cooldown") reads the value from settings; it is never hard-coded.
- **Latest request wins:** when a selection changes quickly (pair, filter, period), only the newest response is shown; older ones are cancelled or ignored.
- **Page title:** the browser tab shows the state, for example "● Running · Trading Agent", "⏸ Paused · Trading Agent", "⛔ Killed · Trading Agent".

## Phone width

The `-390.png` screenshots show each screen at 390 px. Cards stack, wide tables scroll inside their card, and no screen scrolls sideways.

## Logs screen

- **Level chips** use one colour pair each, everywhere a level appears (filters, problems, log lines, surrounding lines): Debug grey, Info blue (`#7FB8FF`), Warning amber, Error red, Critical deep red with a pale label. A level filter that is off has a dashed outline and a hollow dot.
- **Context chips:** cycle ids are blue and open the trace; error ids are red; pairs and other ids are neutral.
- **An opened line** has a 3 px left border in its level's colour. "What this means" and "What to do" come first, because they answer the question before the traceback does.
- **Trace view:** a blue banner states the cycle, its time span, duration, pairs and model calls, with "Back to warnings and errors". Every level is shown; the level filters show all on.
- **Phone width:** time and level chip on one row, the message full width below, then the chips. Recurring problems stack the same way, and the review state and button sit under the problem. The traceback and stored line scroll sideways inside their box instead of wrapping.
- **Times** are Amsterdam with milliseconds; UTC on hover.

## Which PR builds which screens

Follows the spec's build order. A PR builds only the screens listed for it.

| PR | Screens and parts |
|---|---|
| 1 Skeleton | AppShell, Sidebar, Header, phone top bar, tokens, fonts, all components on `/dev/components`; empty Dashboard and Settings |
| 2 Market data | none (backend only) |
| 3 Paper engine | none (backend only) |
| 4 API and GUI | Dashboard (header figures, cycle strip, tiles, open positions with bars, chart, latest decision with regime chip, today's decisions, Markets), Decision drawer, Indicators (incl. regime strip and band), Performance, Settings (markets, risk limits, decision model, data and paper account, data sources), States: first run, stale data, killed, kill acknowledgement, stop and flatten, reset, delete all data |
| 5 OpenAI decider | LLM inspector, Settings → decision model and monthly cost cap, States: re-run dialog, cost cap banner |
| 6 Execution modes and polish | States: approval queue, browser notification, toasts, shock brake banner; Settings → notifications; Needs you card and header chip; outcome section in the drawer; inspector scorecard |
| 7 Understanding and safety | Dashboard: "This cycle" line, morning briefing, risk budget card, per-coin pause; Readiness; Settings → learning and explanations, live readiness, risk throttle and correlation fields; glossary tooltips |
| 8 Evidence | Ask panel, Insights, What-if, Reviews, Settings → shadow strategies; States: What-if running, Insights before data, Ask limit reached |
| 9 Market context collector | Settings → data sources: OKX market data and Deribit options data rows (status, days stored, on/off); States: source stale row |
| 10 Sentiment block | Decision drawer: Market positioning section and News flag gate row; Dashboard: News flag row in the risk budget, banner and quiet-line text; Settings → risk limits: Event flags (news flag); Insights: Positioning slice; States: news flag banner and set-flag dialog |
| 11 Macro calendar | Settings → risk limits: Event flags (macro event gate); Decision drawer: Macro event gate row; What-if: macro event gate option; States: macro event banner |
| 12 Limit entries | Settings → market data & paper account: taker and maker fee, Entry orders (Market / Limit); Settings → shadow strategies: Shadow B row with fill rate; States: Decision drawer footer for a limit entry (waiting, filled, unfilled) |
| 15 Error handling, logging and Logs screen | Logs (all of it, both widths, and the trace view); the Logs item in every sidebar under Control after Readiness; Errors: every page in `Errors.dc.html` (the static pages as files in `deploy/errors/`); card errors showing the error id; Needs you: the degraded item; Cycles card: an error slot links to the cycle's trace; Decision drawer: "Logs for this decision" |

Blue **v0.2** tags in the screens and screenshots mark what v0.2 adds. They are for the
handoff only: do not render them in the app.

## Visual check in every UI PR

1. Add a Playwright test that opens each screen the PR touched with fixed sample data and takes screenshots at 1440 × 900 and 390 × 844.
2. Save them in `docs/screenshots/pr-<n>/`, never in `design/`, and show each one in the PR description next to the matching reference PNG from `design/screenshots/`.
3. List deliberate differences under "Design differences".

## When the design changes

The files in `screens/` and `screenshots/` are updated together, and the item's file in `changes/` gets a dated section (`docs/changes/README.md` → Design notes; `CHANGES.md` up to item 023). Treat each section as a small task and mention it in the PR.
