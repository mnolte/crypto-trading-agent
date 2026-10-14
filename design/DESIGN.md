# Trading Agent v0.1 — Design handoff

This folder is the visual reference for the frontend. Read it together with the
build spec ("Trading Agent v0.1 — Build Spec"). The spec says what the app does;
this folder says what it looks like.

```
design/
  DESIGN.md          this file
  tokens.css         colours, type, spacing and sizes as CSS variables
  screens/*.dc.html  the 12 design screens as HTML (exact styles and wording)
  screenshots/*.png  how each screen renders, at 1440 px wide (Decision: 560 px)
  CHANGES.md         what changed in the design, newest first
```

## Ground rules

1. **The spec wins on behaviour, the design wins on look and wording.** If the two
   disagree, do not guess: build what the spec says, and list the difference in the
   PR description under "Design differences".
2. **The HTML is a reference, not code to copy.** Rebuild every screen as React +
   TypeScript components. Do not import, iframe or convert the `.dc.html` files.
3. **Use the tokens.** Copy `tokens.css` into `frontend/src/styles/` and use the
   variables. No hex values inside components.
4. **Copy the wording as written.** Labels, button text, empty states and tooltips are
   deliberate plain language. Change wording only if the spec requires it.
5. **All numbers in the design are sample data.** Every value comes from the API or
   the WebSocket snapshot. The frontend formats numbers; it never calculates money.
6. **Fonts are bundled.** IBM Plex Sans for text, IBM Plex Mono for every number.
   Install `@fontsource/ibm-plex-sans` and `@fontsource/ibm-plex-mono`; no Google
   Fonts link, no network fonts.

## How to read a `.dc.html` file

The screens use a small template format. Only these parts matter:

- **Inline `style` attributes**: the exact colours, spacing, sizes and radii. This is
  the main thing to take from the files.
- **Text in the markup**: the exact wording.
- `{{name}}`: a value filled in from the script at the bottom of the file. In the
  real app it comes from the API.
- `<sc-for list="{{items}}" as="item">`: a repeated row or card (a `.map()` in React).
- `<sc-if value="{{cond}}">`: shown only when the condition holds.
- `<script type="text/x-dc">`: builds the **sample data** and the simple
  interactions of the mock-up (tab switching, sidebar collapse). Read it to see
  the shape of the data each screen needs. Do not reuse it.
- `support.js` and `<x-dc>` / `<helmet>`: the design tool's runtime. Ignore them.

The screenshots show the rendered result. When a screenshot and the HTML seem to
disagree, the HTML is right.

## Layout rules

- Dark only. The page background is `--bg`; cards are `--panel` with a 1 px `--line`
  border and `--radius-card`.
- Layout works from **1100 px wide** up to 1440 px and beyond. Content area max width
  1440 px.
- App shell: collapsible sidebar (232 px, collapsed 72 px icon rail with tooltips,
  choice remembered per browser) + a 76 px top bar per screen with the title and that
  screen's actions.
- Every clickable element is at least **44 px** high (`--hit`), except compact chips
  inside tables.
- Numbers are right-aligned in tables and always in IBM Plex Mono.
- Up and down colours are always paired with a sign (+ / −) or an arrow, never colour
  alone.
- Card headings: 14 px, weight 600, uppercase, `--text-muted`, letter-spacing 0.06em.
- Terms from `glossary.yaml` get a dotted underline and a tooltip when plain-language
  mode is on (see "Open risk" on the Dashboard).

## Components to build first

Build these in one PR, before any screen, on a hidden page `/dev/components` that shows
every component in every state with sample data. The screens are then built only from
these parts.

| Component | Where it appears | Notes |
|---|---|---|
| AppShell + Sidebar | every screen | Ask button on top, groups Monitor / Learn / Control, status at the bottom, collapse button |
| TopBar | every screen | title left, actions right, wraps on narrow widths |
| Button | everywhere | primary (amber), secondary, danger, ghost; 44 px; icon + label |
| StatusPill | top bar, sidebar | Running / Paused / Killed / Error, with a dot |
| ErrorBadge | top bar | count of errors in 24h, links to the filtered activity log |
| SegmentedControl | Paper/Live, 1h/4h/1d, periods, Weekly/Briefings | one selected segment |
| PairTabs + More menu | Dashboard, Indicators | first 6 active pairs as tabs, the rest in a searchable menu, pause clock badge |
| Card | everywhere | panel + optional uppercase heading + header actions |
| KpiTile | Dashboard, Reviews | label, big mono value, sub-line, optional bar |
| Meter (progress bar) | risk budget, readiness, exposure, drawdown | value vs limit, colour by meaning |
| ActionBadge | decisions | BUY, SELL, HOLD, SKIP |
| Chip | signals, regime, citations, statuses | neutral / up / down / amber / sideways variants |
| DataTable | positions, trades, inspector, markets, insights | mono numbers, right-aligned, horizontal scroll box when narrow |
| Banner | States board | info (blue), warning (amber), danger (red); text + actions |
| Dialog | States board | modal, Escape closes, destructive button never default, typed confirmation variant |
| Drawer | Decision detail | 560 px from the right |
| SidePanel | Ask the agent | 520 px from the right over the dimmed current screen |
| GlossaryTerm | everywhere | dotted underline + tooltip |
| FormField | Settings, What-if | label above, 44 px input, unit, validation message |
| EmptyState | Insights before data, inspector, reviews | short text + what to do or when it fills |
| Charts | Dashboard, Indicators, Performance, What-if, Insights | TradingView Lightweight Charts for price and indicator panes; simple SVG is fine for small charts (calibration, sparklines) |

## Screens

| Screen | Design file | Spec section |
|---|---|---|
| Dashboard | `Main.dc.html` | GUI screens → Dashboard; User features (quiet line, briefing, risk budget, per-coin pause) |
| Decision detail (drawer) | `Decision.dc.html` | GUI screens → Decision detail |
| Performance | `Performance.dc.html` | GUI screens → Performance |
| Settings | `Settings.dc.html` | GUI screens → Settings; Settings, secrets and persistence |
| States and dialogs | `States.dc.html` | banners, dialogs, approval queue, notification |
| Indicators | `Indicators.dc.html` | GUI screens → Indicators; Indicator definitions (regime) |
| LLM inspector | `Inspector.dc.html` | GUI screens → LLM inspector; Outcome tracking |
| Insights | `Insights.dc.html` | User features → Insights |
| What-if | `WhatIf.dc.html` | User features → What-if simulator |
| Ask the agent (panel) | `Ask.dc.html` | User features → Ask the agent |
| Reviews | `Reviews.dc.html` | User features → Weekly review, Daily briefing |
| Readiness | `Readiness.dc.html` | User features → Live-readiness checklist |

The States board is a catalogue, not a page: each banner, dialog and card on it belongs
to the screen named in its label.

## Which PR builds which screens

Follows the spec's build order. A PR builds only the screens listed for it.

| PR | Screens and parts |
|---|---|
| 1 Skeleton | AppShell, Sidebar, TopBar, tokens, fonts, all components on `/dev/components`; empty Dashboard and Settings |
| 2 Market data | none (backend only) |
| 3 Paper engine | none (backend only) |
| 4 API and GUI | Dashboard (KPIs, chart, latest decision with regime chip, positions, activity), Decision drawer, Indicators (incl. regime strip and band), Performance, Settings (markets, risk limits, decision model, data and paper account, data sources), States: first run, stale data, killed, kill acknowledgement, stop and flatten, reset, delete all data |
| 5 OpenAI decider | LLM inspector, Settings → decision model and monthly cost cap, States: re-run dialog, cost cap banner |
| 6 Execution modes and polish | States: approval queue, browser notification, shock brake banner; Settings → notifications; error badge; outcome section in the drawer; inspector scorecard |
| 7 Understanding and safety | Dashboard: "This cycle" line, morning briefing, risk budget card, per-coin pause; Readiness; Settings → learning and explanations, live readiness, risk throttle and correlation fields; glossary tooltips |
| 8 Evidence | Ask panel, Insights, What-if, Reviews, Settings → shadow strategies; States: What-if running, Insights before data, Ask limit reached |
| 9 Market context collector | Settings → data sources: OKX market data and Deribit options data rows (status, days stored, on/off); States: source stale row |
| 10 Sentiment block | Decision drawer: Market positioning section and News flag gate row; Dashboard: News flag button, banner and quiet-line text; Settings → risk limits: Event flags (news flag); Insights: Positioning slice; States: news flag banner and set-flag dialog |
| 11 Macro calendar | Settings → risk limits: Event flags (macro event gate); Decision drawer: Macro event gate row; What-if: macro event gate option; States: macro event banner |

Blue **v0.2** tags in the screens and screenshots mark what v0.2 adds. They are for the
handoff only: do not render them in the app.

## Visual check in every PR

1. Add a Playwright test that opens every screen the PR touched with fixed sample data
   (from the simulated market or a fixture) and takes a screenshot at 1440 × 900 and
   1100 × 900.
2. Attach those screenshots to the PR, next to the matching file in
   `design/screenshots/`.
3. List any deliberate differences under "Design differences" in the PR description.

## When the design changes

The designer updates the files in `screens/` and `screenshots/` and adds an entry to
`CHANGES.md`. Treat each entry as a small task: update the affected components and
screens, and mention the entry in the PR.
