# CLAUDE.md

Read `AGENTS.md` first: its rules apply to Claude Code in full. This file only adds what is specific to Claude.

## Context

- Trading Agent is a paper-only crypto trading agent on Bitvavo EUR markets. Behaviour is in `docs/SPEC.md`, look and wording in `design/`, strategy research in `docs/RESEARCH.md` and `scripts/regime_history.py`.
- Its sibling, Stock Trading Agent (`mnolte/stock-trading-agent`), leads on design and on shared screens. When a screen or rule exists in both, start from the stock version and adapt it to crypto (24/7 cycles, Bitvavo, euros, paper broker).
- Marc builds with Devin for implementation and Claude for architecture, reviews, research and spec work. Expect to be asked to review PRs, check code against the spec, and update docs.
- Build steps are numbered in the spec ("PR 14"); GitHub pull request numbers are different. Always name the build step.

## When reviewing a PR

- Check it against the spec sections for that build step (`docs/README.md` has the map) and against the `AGENTS.md` hard rules.
- Look specifically for: network calls in tests, `datetime.now()` in engine code, floats for money, hard-coded thresholds, any exchange trading endpoint or exchange API key, edited prompt files that are in use, processes killed by name, and anything that deletes `data/`, a database, a backup or a log outside a test's own temporary folder.
- Also check: screenshots or other files written into `design/` (they belong in `docs/screenshots/pr-<n>/`); merged migrations that were edited; tests that share one database; secrets in logs, fixtures or API responses.
- Check that risk math (stop distance, risk, R/R, fee gate) comes from the one shared function.
- Report findings as: blocking, should fix, nice to have. Quote file and line. End with a reply Marc can paste to the author.

## When changing docs

- Work on a `spec/<topic>` branch from the latest `main` and open a pull request with the label `spec-change`. Never push to `main` and never merge; Marc does.
- Never edit the root `README.md`; Devin owns it.
- Write each new kickoff as `docs/kickoffs/pr-<n>.md` (the build step number). `docs/DEVIN_KICKOFF.md` is the archive up to PR 13.
- Spec changes go to `docs/SPEC.md` only when Marc asks, plus a dated entry in `docs/CHANGES.md`.
- Design changes go to `design/screens/` and `design/screenshots/` (1440 px, and `-390.png` for screens with the shell), plus an entry in `design/CHANGES.md`. The design canvas "Trading Agent GUI" is updated in the same step, so the two never drift.
- Prompt changes are new files in `backend/prompts/` (`decision.v3.md`), never edits to a version in use.
- `docs/SPEC.md` in this repo is the source of truth. The Claude Doc "Trading Agent v0.1 — Build Spec" is an older copy and is only updated when Marc asks.
- Strategy changes follow the rule in the spec's Decisions: an idea goes into the spec only when it beats the static BTC + cash mix at the same maximum drawdown in training and test, with a sensitivity grid that agrees.

## Style

- Plain, short sentences. Concrete numbers over adjectives.
- Name assumptions explicitly and list open questions instead of guessing.
