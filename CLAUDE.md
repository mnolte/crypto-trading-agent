# CLAUDE.md

Read `AGENTS.md` first: its rules apply to Claude Code in full. This file only adds what is specific to Claude.

## Context

- Trading Agent is a paper-only crypto trading agent on Bitvavo EUR markets. Behaviour is in `docs/SPEC.md`, look and wording in `design/`, strategy research in `docs/RESEARCH.md` and `scripts/regime_history.py`.
- Its sibling, Stock Trading Agent (`mnolte/stock-trading-agent`), leads on design and on shared screens. When a screen or rule exists in both, start from the stock version and adapt it to crypto (24/7 cycles, Bitvavo, euros, paper broker).
- Marc builds with Devin for implementation and Claude for architecture, reviews, research and spec work. Expect to be asked to review PRs, check code against the spec, and update docs.
- Work items are numbered `NNN` (`docs/work-items.md`); build plan item 14 ("PR 14" in the spec) is item 014. GitHub pull request numbers are different and always written `#n`. Always name the item.

## When reviewing a PR

- Check it against the spec sections for that item (`docs/README.md` has the map for build plan items, the kickoff for newer ones) and against the `AGENTS.md` hard rules.
- Look specifically for: network calls in tests, `datetime.now()` in engine code, floats for money, hard-coded thresholds, any exchange trading endpoint or exchange API key, edited prompt files that are in use, processes killed by name, and anything that deletes `data/`, a database, a backup or a log outside a test's own temporary folder.
- Also check: screenshots or other files written into `design/` (they belong in `docs/screenshots/NNN/`, or `pr-<n>/` for items up to 020); the branch, PR title, commits and register row follow `docs/work-items.md`; merged migrations that were edited; tests that share one database; secrets in logs, fixtures or API responses.
- Check that risk math (stop distance, risk, R/R, fee gate) comes from the one shared function.
- Report findings as: blocking, should fix, nice to have. Quote file and line. End with a reply Marc can paste to the author.

## When changing docs

- Every piece of work with a kickoff is a work item `NNN-type-slug` (`docs/work-items.md`): take the next free number and add its row (status `planned`) in the spec PR.
- Work on a `spec/NNN-slug` branch from the latest `main` and open a pull request titled `[NNN] spec: Title` with the label `spec-change`. Never push to `main` and never merge; Marc does.
- Never edit the root `README.md`; Devin owns it.
- Write each new kickoff as `docs/kickoffs/NNN-type-slug.md` with the first line `# NNN · type · Title`, and refer to items as `NNN` or `[NNN]`, GitHub PRs as `#n`. Older kickoffs keep their names (`pr-14.md`, `pr-15.md`); `docs/DEVIN_KICKOFF.md` is the archive up to item 013.
- Spec changes go to `docs/SPEC.md` only when Marc asks, plus a dated entry in `docs/CHANGES.md`.
- Design changes go to `design/screens/` and `design/screenshots/` (1440 px, and `-390.png` for screens with the shell), plus an entry in `design/CHANGES.md`. The design canvas "Trading Agent GUI" is updated in the same step, so the two never drift.
- Prompt changes are new files in `backend/prompts/` (`decision.v3.md`), never edits to a version in use.
- `docs/SPEC.md` in this repo is the source of truth. The Claude Doc "Trading Agent v0.1 — Build Spec" is an older copy and is only updated when Marc asks.
- Strategy changes follow the rule in the spec's Decisions: an idea goes into the spec only when it beats the static BTC + cash mix at the same maximum drawdown in training and test, with a sensitivity grid that agrees.

## Style

- Plain, short sentences. Concrete numbers over adjectives.
- Name assumptions explicitly and list open questions instead of guessing.
