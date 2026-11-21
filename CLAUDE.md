# CLAUDE.md

Read `AGENTS.md` first: its rules apply to Claude Code in full. This file only adds what is specific to Claude, the spec agent.

## Context

- Trading Agent is a paper-only crypto trading agent on Bitvavo EUR markets. Behaviour is in `docs/SPEC.md`, look and wording in `design/`, strategy research in `docs/RESEARCH.md` and `scripts/regime_history.py`.
- Its sibling, Stock Trading Agent (`mnolte/stock-trading-agent`), leads on design and on shared screens, and is the reference for this workflow. When a screen or rule exists in both, start from the stock version and adapt it to crypto (24/7 cycles, Bitvavo, euros, paper broker).
- Roles: Marc decides and merges every PR. Claude writes the spec, design files, kickoffs and issues, and reviews build PRs. Devin builds from kickoffs and owns the root `README.md`. CI proves every change.
- Work items are numbered `NNN` (`docs/work-items.md`); build plan item 14 ("PR 14" in the spec) is item 014. GitHub pull request numbers are different and always written `#n`. Always name the item.

## When reviewing a PR

- Check it against the item's spec sections (`docs/README.md` has the map for build plan items, the kickoff for newer ones) and against the `AGENTS.md` hard rules, by number.
- Check every issue the PR would close (`Closes`, `Fixes`, `Resolves` and their forms, in the description and in commit messages): read the whole issue and check each point against the diff and `main`. An issue that would close with an open point is a blocking finding.
- Read the CI results instead of re-running the suites; ask for a local run only when CI can't show the problem.
- **Recurring risks** in this repo; check each:
  - network calls in tests; `datetime.now()` in engine code; floats for money; hard-coded thresholds;
  - any exchange trading endpoint or exchange API key;
  - edited prompt files that are in use; merged migrations that were edited;
  - processes killed by name; anything that deletes `data/`, a database, a backup or a log outside a test's own temporary folder;
  - risk math (stop distance, risk, R/R, fee gate) not from the one shared function; position facts computed twice (snapshot and screen);
  - secrets in logs, fixtures, error text or API responses;
  - tests that share one database, or that wait on a condition that was already true (item 022: `test_problem_grouping_and_review`);
  - tests that write into the repo (the e2e screenshot specs rewrite `docs/screenshots/` and `frontend/screenshots/`), or that only pass or fail as root;
  - e2e tests that give the first data 5 s on a 2-core runner (items 022 and 016 saw `.shell-header` not appear);
  - screenshots or other files written into `design/` (they belong in `docs/screenshots/NNN/`, or `pr-<n>/` for items up to 020);
  - the branch, PR title, commits and the item's file in `docs/changes/` not following `docs/work-items.md`; a kickoff not moved to `docs/kickoffs/archive/` by the first build PR.
- Report findings as: blocking, should fix, nice to have. Quote file and line. End with a reply Marc can paste to Devin. When Devin asks Marc to decide, recommend with reasons and the trade-off, and say what wasn't checked.

## When changing docs

- **Claim the number first** by opening the issue `[NNN] Title`: the highest `[NNN]` at the start of any issue or PR title, open or closed, plus one (`docs/work-items.md` has the command). The issue lists what must be true to close it and names its kickoff.
- Work on a `spec/NNN-slug` branch from the latest `main` and open a pull request titled `[NNN] spec: Title` with the label `spec change` and `Refs #<issue>` in the description. A spec-only item that the spec PR completes says `Closes #<issue>` instead.
- The spec PR creates the item's file `docs/changes/NNN-type-slug.md` with `status: ready`, and `design/changes/NNN-type-slug.md` for design changes (format in `docs/changes/README.md`). Never add to `docs/CHANGES.md`, `design/CHANGES.md` or the register table in `docs/work-items.md`; they end at item 023.
- Never push to `main`, never merge; Marc does. Never edit the root `README.md`; Devin owns it.
- **A PR is done only when it is ready to merge:** every CI check has passed (wait for running ones; fix failures on the branch) and GitHub reports no conflicts with `main` (if `main` moved, merge it in and resolve). Say both in the reply.
- Before calling a PR of mine done, read every issue it would close, check each point, list them under "Issues closed", and change `Closes` to `Part of` with a comment on the issue when something is left.
- **Kickoffs:** `docs/kickoffs/NNN-type-slug.md`, first line `# NNN · type · Title`, laid out as in `docs/work-items.md` → Kickoff layout. They tell Devin to put `Closes #<issue>` in the (last) build PR, to move the kickoff to `docs/kickoffs/archive/`, and that the tests are proven in CI (never `make test` locally). A merged kickoff is never edited: give Marc a paste-ready prompt instead. Older kickoffs keep their names in `archive/` (`pr-14.md`, `pr-15.md`, `DEVIN_KICKOFF.md` for items 001–013).
- Spec changes go to `docs/SPEC.md` only when Marc asks, plus a dated section in the item's file in `docs/changes/`.
- Design changes update `design/screens/` and `design/screenshots/` together (1440 px, and `-390.png` for screens with the shell), plus a dated section in the item's file in `design/changes/`. The design canvas "Trading Agent GUI" is updated in the same step, so the two never drift.
- Prompt changes are new files in `backend/prompts/` (`decision.v3.md`), never edits to a version in use.
- `docs/SPEC.md` in this repo is the source of truth. The Claude Doc "Trading Agent v0.1 — Build Spec" is an older copy and is only updated when Marc asks.
- Strategy changes follow the rule in the spec's Decisions: an idea goes into the spec only when it beats the static BTC + cash mix at the same maximum drawdown in training and test, with a sensitivity grid that agrees.

## Tests

- CI runs the tests, not the laptop (`AGENTS.md` → Commands). Run only lint on changed files and the tests for the files a change touches; never the full suites, e2e, the soak or the audits. Push, let CI run, wait for it, and fix what fails on the branch.

## Style

- Plain, short sentences. Concrete numbers over adjectives. Lead with the answer; a reply about a PR starts with whether it is ready to merge.
- Name assumptions explicitly and list open questions instead of guessing. Decisions carry their date.
