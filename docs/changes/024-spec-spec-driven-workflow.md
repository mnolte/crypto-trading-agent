---
item: 024
type: spec
title: Spec-driven workflow
status: merged
issue: 41
kickoff: —
spec_prs: []
build_prs: []
---

# 024 · spec · Spec-driven workflow

## 2026-11-19 · spec PR

Marc, 19 Nov: this repo follows the project's workflow (project doc `claude/workflow.md`; reference `mnolte/stock-trading-agent`). His answers: Devin builds and owns the root `README.md`; the design stays, at 1440 and 390 px; locally only lint on changed files and the tests for the change; the 11 hard rules stay, numbered. It extends what items 021 (names) and 022 (CI, label gate) added.

- **Claiming a number:** open the issue `[NNN] Title` first, one above the highest `[NNN]` in any issue or PR title (`docs/work-items.md` → Claiming a number). Issues close themselves through `Closes #n` in the (last) build PR; a PR closes only issues it resolves, point by point (`AGENTS.md` → How to work).
- **One file per work item** from item 024: `docs/changes/NNN-type-slug.md` (register fields and dated notes) and `design/changes/` for design (`docs/changes/README.md`). `docs/CHANGES.md`, `design/CHANGES.md` and the register table in `docs/work-items.md` end at item 023; open PRs for items up to 023 (#34, #35) still update them. `scripts/work_items.py` prints the register; `--check` runs in CI as a test.
- **Kickoffs move to `docs/kickoffs/archive/`** with the first build PR; the three finished ones (`DEVIN_KICKOFF.md`, `pr-14.md`, `pr-15.md`) moved in this PR. `docs/work-items.md` → Kickoff layout says how a kickoff is laid out.
- **Tests run in CI, not on the laptop** (`AGENTS.md` → Commands): locally only lint and the tests for changed files; never the full suites, e2e, the soak or the audits. CI now also runs the slow tests and the dependency audits on every PR push.
- **The label gate** reads the label `spec change` (was `spec-change`), runs on label changes, needs a changed file in `docs/changes/` for spec, rule, prompt or env changes and one in `design/changes/` for design screen changes, and lets an unchanged move of a kickoff into the archive through. Tests in `backend/tests/ci/`.
- `AGENTS.md` (sources of truth, How to work, Commands, hard rules numbered 1–11), `CLAUDE.md` (reviews with the recurring risks, docs rules, tests, style), `docs/README.md` (rules 1–12), `.github/pull_request_template.md` (new).
- **Not changed:** the spec, the design, the prompts, the map in `docs/README.md`, and the register rows up to 023.
