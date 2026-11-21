# 025 · feat · Settings sections

Kickoff for Devin. Start the session with: "Build docs/kickoffs/025-feat-settings-sections.md."

Settings work as in Stock Trading Agent: one section at a time with a grouped section list, one row per setting with its default, Find a setting and Changed from default, a save bar with tighter and looser tags and a required reason, numbered settings versions with their reason in History, and the money next to each limit computed in the backend. What each setting does doesn't change. Spec: `docs/SPEC.md` → v0.3 → Settings screen (new), Settings, secrets and persistence, API. Design: `design/screens/Settings.dc.html` with `Settings.png`, `Settings-390.png`, `Settings-390-save.png`, `Settings-history.png`; `design/DESIGN.md` → Settings screen. Issue #44.

```text
025 (Settings sections). Goal: the Settings screen and the settings API work
as v0.3 → Settings screen describes, so every change shows whether it makes
the agent riskier, needs a reason, and can be traced to a numbered version.
Decisions, gates, sizing, exits and the cycle don't change. Start from the
latest main (it has 016's design, #43, and this kickoff's PR).

Names (docs/work-items.md): branch feat/025-settings-sections, PR titles
"[025] feat: Settings sections (backend)" and "[025] feat: Settings sections
(screen)", commits start with [025], screenshots in docs/screenshots/025/.
Update docs/changes/025-feat-settings-sections.md in each PR: add the PR to
build_prs, status building in the first and merged in the last, and a dated
section "## YYYY-MM-DD · build PR #n" saying what changed in behaviour.

Read first:
1. docs/changes/025-feat-settings-sections.md and
   design/changes/025-feat-settings-sections.md
2. docs/SPEC.md → v0.3 → Settings screen (all of it); Settings, secrets and
   persistence; Risk gates (the limits the summary describes); the API table
3. design/DESIGN.md → Settings screen and Screens; design/screens/
   Settings.dc.html and its four screenshots (texts are final; open the file
   with #markets, #history and so on to see each section); States.dc.html
   (dialogs, card states)
4. AGENTS.md → hard rules 2 (money in Python, as Decimal: the frontend does
   no money arithmetic, so every "= €…" comes from the preview endpoint),
   3 (thresholds from config) and 8 (secrets never leak)
5. Stock Trading Agent's frontend/src/pages/settings/ for reference only: the
   same pattern, its code may be read but not copied in blindly

PRs: two. The first (backend) says "Part of #44" and moves this kickoff
unchanged to docs/kickoffs/archive/ with git mv. The second (screen) says
"Closes #44" after you have checked every point of the issue against main
plus the PR and listed them under "Issues closed".

What exists today (main on 21 November):
- Settings (backend/app/models.py:452) is one document; PUT /api/settings
  (api/routes.py:163) replaces it whole; repo.save_settings (repo.py:76)
  writes one settings_history row per changed leaf (key, old, new, no reason)
  and bumps settings.version.
- Other places call repo.save_settings directly: main.py:447 (TA_DATA_SOURCE
  at start), api/evidence.py:204 (What-if apply) and :303 (promote shadow),
  api/sources.py:229 (source on/off) and :344 (market activate/deactivate),
  api/notifications.py:61 (notifications PUT).
- Readiness (features/readiness.py:63) and What-if (features/whatif.py:188)
  read settings_history; they must keep working.
- The models have no ranges: a value is only type-checked.
- frontend/src/pages/Settings.tsx (1,304 lines) is one long page;
  e2e/settings-save.spec.ts and limit-orders.spec.ts use it.

Steps, PR 1 (backend), one commit each:

1. Ranges. Add Field(ge=…, le=…) to every numeric setting as in the table
   below, plus model validators for the cross-field rules. A value outside
   its range is a 422 with the field and "0.1 to 2" style message.
2. Directions. backend/app/settings_directions.py: DIRECTIONS maps every
   dotted key in the table below to "up" (raising is looser), "down"
   (lowering is looser), "off" (switching off is looser), "longer" (a longer
   list is looser), "toward_auto" (execution_mode: manual → supervised →
   auto is looser) or "neutral", each with its one-line reason. A key in
   risk, rules, model, paper, market, readiness or notifications.events, or
   execution_mode, veto_window_s, cycle_offset_s, active_markets, without an
   entry counts as looser (fails closed). CONFIRM_ALWAYS: model.decider,
   model.model_id, model.prompt_version, and any notifications.events.*
   channel going off.
3. Versions. An Alembic revision adds settings_versions: version (int, PK),
   at, reason (text), source (screen, promote, whatif, action, migration),
   doc (JSON), changes (JSON list of key, old, new, direction). Add a
   nullable version column to settings_history. At the first start after the
   revision, if settings_versions is empty, write the current document as
   version settings.version with reason "Settings when versions started
   (item 025)" and source migration.
4. Settings service. backend/app/settings_service.py with
   save(session, draft, reason, source, confirm_looser) → version:
   validate; diff against the active document; tag each change from
   DIRECTIONS; refuse a reason shorter than 3 or longer than 200 characters;
   raise LooserNeedsConfirm(changes) when a change is looser or in
   CONFIRM_ALWAYS and confirm_looser is false; else write settings (version
   + 1), settings_versions and the settings_history rows with that version,
   in one transaction. Replace every repo.save_settings call above with it:
   - PUT /api/settings: source screen, the reason from the request;
   - What-if apply: source whatif, "Applied What-if scenario '<name>'";
   - promote: source promote, "Promoted <shadow name>";
   - source on/off: source action, "Turned on <source>" / "Turned off …";
   - market activate/deactivate: source action, "Activated <pair>" /
     "Deactivated <pair>";
   - notifications PUT (now SMTP only, from Test and save): source action,
     "Email settings tested and saved";
   - TA_DATA_SOURCE at start: source action, "TA_DATA_SOURCE=<value> at
     start".
   The actions with a fixed reason pass confirm_looser=true when the API call
   carries {"confirm": true}; activating a market is looser ("longer"), so
   POST /api/markets/{pair}/activate without it answers 409 like PUT does.
5. API (regenerate openapi.json and the frontend types):
   - GET /api/settings: unchanged.
   - GET /api/settings/meta: version; per dotted key: section (the ids in
     the design: markets, execution, risk, exits, rules, broker, model,
     shadows, notify, display, keys, ready, logs, paper), default, min, max,
     step, unit, choices, direction, fixed.
   - POST /api/settings/preview: body a draft document; returns equivalents
     ({key: "€83.30"} for every % of equity, max costs and the kill level),
     summary (the three Risk limits sentences as data: loss at stop, at the
     throttled risk, positions at full risk and what the next one gets, daily
     stop in €, kill level in € and the peak), changes (key, old, new,
     direction) and errors ({key: message}). Saves nothing. Money with
     Decimal, rounded to cents like every other euro amount in the app
     (quantize(Decimal("0.01")), half to even), from equity, the day's start
     equity and the peak in the current snapshot, through the same
     functions the gates and sizing use (no second copy of the risk maths).
   - PUT /api/settings: body {settings, reason, confirm_looser}. 422 for
     field errors or the reason; 409 {"error": {"code":
     "looser_needs_confirm", "changes": […]}} in the 015 error shape; 200
     {version, settings}.
   - GET /api/settings/history?cursor=: versions newest first (version, at,
     reason, source, changes with direction), then settings_history rows
     without a version, 50 per page.
   - The settings WebSocket broadcast carries the new version.

Steps, PR 2 (screen):

6. Routes /settings/:section (plain /settings opens the last section used,
   kept in localStorage, else risk). The section list in five groups and the
   Section select below 760 px, as in the design; previous and next links.
7. Rows from GET /api/settings/meta plus a section file per section in the
   frontend with the labels and explanations from Settings.dc.html. Every
   key in meta with a section has exactly one row; a Vitest test compares
   the frontend's row keys with meta's keys (the five keys the spec leaves
   off the screen excepted).
8. The draft: changes kept across sections; each changed row highlighted
   with "Changed from … · Reset"; the counts in the section list; Find a
   setting (name, explanation, section) and Changed from default (saved vs
   default from meta) across sections, opening an Advanced fold with a
   match; POST /api/settings/preview 300 ms after typing stops, its errors
   shown under the field with the range, Save disabled while one exists.
9. The save bar per the design (desktop and the one-line phone bar), the
   reason field, Discard, "Save as v<n>". On 409 a dialog from
   States.dc.html → confirm: "Raises risk per trade 1.0 → 1.2%. Save?"
   listing each looser change; Save again with confirm_looser true.
   Leaving /settings with a draft asks first (router blocker and
   beforeunload).
10. Sections that keep today's content until their own item: Markets keeps
    today's market table under the thresholds until PR 17, with "Open
    Markets" shown as unavailable and the reason as text; Shadows keeps
    today's shadow card with Start, Stop and Promote until PR 18. Keys and
    SMTP keep their own buttons; they never enter the draft.
11. History section (versions table with tags and reasons, "Show earlier"
    paging, the older per-setting rows after them), Schedule (read only),
    Paper account (existing buttons moved there). Delete the old one-page
    layout.

Config ranges (min to max; cross-field rules after the table):
risk.min_rr 1–5 · max_cost_pct_of_risk 5–100 · risk_per_trade_pct 0.1–2 ·
throttled_risk_per_trade_pct 0.05–2 · total_open_risk_pct 0.5–10 ·
max_exposure_pct 10–100 · min_trade_eur 5–1000 ·
cooldown_after_stopout_hours 0–72 · correlated_max_positions 1–10 ·
correlated_threshold 0.5–0.99 · shock_brake_btc_move_pct 1–20 ·
shock_brake_hours 0.5–24 · throttle_consecutive_losses 2–10 ·
throttle_drawdown_pct 1–20 · throttle_lift_drawdown_pct 0–20 ·
daily_loss_limit_pct 0.5–10 · kill_drawdown_pct 2–30 · stop_min_atr
0.1–10 · stop_max_atr 0.1–10 · stop_buffer_atr 0–1 ·
pending_entry_drift_pct 0.1–5 · time_stop_days 1–30
rules.prefilter_min_conds 1–5 · buy_min_conds 1–5 ·
buy_max_atr_above_ema20 0.5–5 · stop_lookback_bars 2–48 · stop_atr_pad 0–1
· stop_max_atr 1–10 · target_max_atr 1–20 · target_rr 1–10
model.daily_call_budget 0–20000 · monthly_cost_cap_eur 0–1000
paper.start_balance_eur 100–1000000 · taker_fee_pct 0–2 · maker_fee_pct
0–2 · slippage_pct 0–2
market.adx_threshold 10–50 · min_avg_daily_volume_eur 100000–100000000 ·
max_avg_spread_pct 0.01–2 · min_daily_candles 30–1000 ·
eligibility_fail_days 1–14 · eligibility_beyond_pct 0–100 ·
eligibility_recover_days 1–14 · history_1h, _4h, _1d 100–5000 ·
structure_min_range_bars 3–100 · stale_grace_s 60–7200
readiness.min_closed_trades 10–1000 · min_days_running 7–365 ·
max_drawdown_pct 1–30 · calibration_gap_pts 1–50
veto_window_s 10–600 · cycle_offset_s 0–600 · learning.ask_daily_limit
0–1000 · learning.insights_min_decisions 5–500 · ui.stop_near_atr 0.5–5 ·
logging.keep_days.* 1–3650 · logging.max_db_mb 100–100000 · timeouts.*
1–300
Cross-field: throttled_risk_per_trade_pct ≤ risk_per_trade_pct ≤
total_open_risk_pct; stop_min_atr < stop_max_atr; throttle_lift_drawdown_pct
< throttle_drawdown_pct < kill_drawdown_pct; daily_loss_limit_pct <
kill_drawdown_pct; rules.prefilter_min_conds ≤ rules.buy_min_conds.

Directions (looser when …):
risk: min_rr down · max_cost_pct_of_risk up · risk_per_trade_pct up ·
throttled_risk_per_trade_pct up · total_open_risk_pct up · max_exposure_pct
up · min_trade_eur down · cooldown_after_stopout_hours down ·
correlated_max_positions up · correlated_threshold up (fewer pairs count as
correlated) · shock_brake_btc_move_pct up · shock_brake_hours down ·
throttle_consecutive_losses up · throttle_drawdown_pct up ·
throttle_lift_drawdown_pct up (lifts sooner) · daily_loss_limit_pct up ·
kill_drawdown_pct up · stop_min_atr down · stop_max_atr up (a wider range) ·
pending_entry_drift_pct up · stop_buffer_atr neutral (where the stop sits;
size follows) · time_stop_days neutral (cuts winners and losers alike) ·
trailing_stop_enabled neutral (locks gains, may exit earlier)
rules: prefilter_min_conds down · buy_min_conds down ·
buy_max_atr_above_ema20 up · stop_max_atr up · target_rr down ·
stop_lookback_bars, stop_atr_pad, target_max_atr neutral
model: daily_call_budget up · monthly_cost_cap_eur up · rules_prefilter off
· decider, model_id, prompt_version neutral and CONFIRM_ALWAYS
paper: taker_fee_pct, maker_fee_pct, slippage_pct down (results look
better than they are) · start_balance_eur, entry_orders neutral
market: adx_threshold down · min_avg_daily_volume_eur down ·
max_avg_spread_pct up · min_daily_candles down · eligibility_fail_days up ·
eligibility_beyond_pct up · eligibility_recover_days down · stale_grace_s up
· history_*, structure_min_range_bars neutral · stablecoin_bases longer is
tighter (shown fixed)
readiness: min_closed_trades down · min_days_running down ·
max_drawdown_pct up · calibration_gap_pts up
notifications.events.*.browser and .email: off
execution_mode toward_auto · veto_window_s down · cycle_offset_s neutral ·
active_markets longer

Tests (each rule a pass and a fail case; no network, each test its own
database, injected time; proven in CI, never run the full suites locally):
- ranges: one value at each end of three ranges passes, one beyond fails
  with the message; each cross-field rule passes and fails;
- directions: every key in the listed sections has an entry (the test fails
  on a new key without one); a key missing from the map counts as looser;
  raising risk_per_trade_pct is looser, lowering it tighter; execution_mode
  manual → auto is looser;
- service: a save writes settings, settings_versions and settings_history
  rows with one version in one transaction (a failure leaves none); a
  reason of 2 characters fails, 3 passes; a looser change without confirm
  raises, with confirm saves; a tighter change saves without confirm; a
  model_id change always asks;
- callers: What-if apply, promote, source on/off, market activate and
  deactivate, SMTP Test and save, and TA_DATA_SOURCE each write a version
  with their source and reason; activating a market without confirm is a
  409; Readiness's loosened note and What-if's settings-change list still
  find their changes;
- migration: the first start writes the base version once; a second start
  writes nothing;
- API: meta has every key with a section; preview returns € amounts equal to
  equity × percentage as Decimal, the summary for the design's values
  (€10,412.80 equity, day start €10,326.60, peak €10,486.20, 0.8% and 2.5%:
  €83.30, €52.06, max costs €20.82, 3 positions and 0.1%, −€258.16,
  €9,647.30) and errors for an out-of-range
  value, and saves nothing; PUT returns 422, 409 and 200 as described;
  history pages across versions and older rows; no response or version
  contains a key or the SMTP password;
- frontend (Vitest): rows cover meta's keys; the draft survives a section
  switch; Find and Changed from default list rows from several sections;
  Save is disabled while preview reports an error; the confirm dialog
  appears on 409 and Save then sends confirm_looser; leaving with a draft
  asks;
- e2e: update settings-save.spec.ts (change risk per trade, see the tag,
  enter a reason, save, see v<n> in History) and limit-orders.spec.ts
  (entry orders now in Paper broker); a looser save shows the confirm.

Screenshots (docs/screenshots/025/, next to design/screenshots/): Settings
#risk with two unsaved changes at 1440 × 900 and 390 × 844, the phone save
bar opened, #history at 1440, the looser confirm dialog. No sideways scroll
at 390 px.

Rules:
- Don't change what any setting does, its default, decisions, gates, sizing,
  exits or the cycle. Defaults stay in backend/app/models.py.
- No money arithmetic in the frontend: amounts come from the preview.
- Keys and the SMTP password never in a settings version, history,
  preview, log or response.
- Don't edit docs/SPEC.md, design/ or backend/prompts/ (ask in the PR).
- Never delete data/, a database or a backup outside a test's own folder.

Done when: CI is green on the last push of each PR and both merge cleanly;
every point of #44 is met and listed under "Issues closed" in PR 2; the
item's file is updated and this kickoff is in docs/kickoffs/archive/.
```

## Why

Marc wants the crypto agent's settings to work like the stock agent's (21 Nov). Today a crypto save replaces the whole document without a reason, and nothing says whether a change makes the agent riskier; the stock pattern makes every change explicit, tagged and traceable to a version that decisions record.

## Assumptions

- The ranges and directions in the kickoff are new; the spec only required that they exist. They follow the stock agent where a setting exists in both (for example risk per trade up is looser) and the spec's meaning elsewhere. Marc can change any of them in this PR's review.
- Activating a market counts as looser and asks once more, like the stock agent's universe switch; deactivating doesn't.
- The Markets table and the shadow card stay in Settings until items 017 and 018, so this item doesn't wait for them.
- Two build PRs, backend first, so the API can be reviewed before the screen.

## Open questions

- Are the ranges right? The tightest calls: risk per trade at most 2%, total open risk at most 10%, kill drawdown at most 30%.
- Should lower fees or slippage in the paper broker really count as looser (they make paper results look better), or be neutral?
