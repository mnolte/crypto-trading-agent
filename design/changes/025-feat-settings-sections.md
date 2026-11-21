# 025 · feat · Settings sections

## 2026-11-21 · spec PR

- `design/screens/Settings.dc.html` rebuilt in Stock Trading Agent's Settings pattern with crypto's settings: 16 sections in five groups, switched by the address fragment (`#risk` when there is none), a section list with Find a setting and Changed from default, one row per setting with its default and the euro amount next to every % of equity, fixed values with a lock, an Advanced fold in Markets, "What these limits mean today" in Risk limits, and the sticky save bar with tighter tags, a reason and "Save as v8" (one line on a phone).
- The scenario follows the Dashboard (Tue 13 Oct 12:34): equity €10,412.80, day start €10,326.60, peak €10,486.20, settings v7, two unsaved tighter changes (risk per trade 1.0 → 0.8%, daily loss limit 3 → 2.5%), and two saved values that differ from their default (max costs 25%, weekly review Sunday 20:00).
- The market table and the shadow controls are no longer drawn: Markets links to the Markets screen (017) and Shadows to Strategies (018). Until those items, the kickoff keeps today's table and card in those sections.
- Screenshots: `Settings.png` (Risk limits), `Settings-390.png`, `Settings-390-save.png` (phone save bar opened, new), `Settings-history.png` (new).
- `design/DESIGN.md`: the Settings row, a new "Settings screen" section and a row in "Which PR builds which screens".
