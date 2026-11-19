<!-- Title: [NNN] type: Title (docs/work-items.md). Branch: type/NNN-slug. -->

## Summary

<!-- Work item: NNN-type-slug (kickoff docs/kickoffs/NNN-type-slug.md). What was built, in a few bullets. -->

Work item: 

<!-- The item's issue: "Closes #n" closes it when this PR merges (last PR of the item); "Part of #n" for an earlier PR; "Refs #n" for a spec PR; delete the line if the item has no issue. -->

Closes #

## Issues closed

<!-- For every issue this PR closes: read the whole issue and list each point
     it asks for with where it is met (file, test). A point that is not done
     means "Part of #n" above plus a comment on the issue, not "Closes"
     (AGENTS.md → How to work). "None" if this PR closes no issue. -->


## Spec files changed

<!-- "Spec files changed: none" or a list with the reason for each.
     Anything matching .github/spec-paths.txt needs the "spec change"
     label; spec, rule, prompt and env changes need a section in the item's
     file in docs/changes/, design screen changes one in design/changes/
     (docs/changes/README.md). -->

Spec files changed: none

## Test plan

<!-- What was tested. The suites run in CI, not locally (AGENTS.md → Commands). -->

- [ ] CI is green on the last push (all checks)
- [ ] Locally: only lint on the changed files and the tests for them (name them)
- [ ] The item's file `docs/changes/NNN-type-slug.md` updated (build_prs, status, a dated section for behaviour, config or prompt changes; the row in `docs/work-items.md` for items up to 023)
- [ ] The kickoff this PR builds moved unchanged to `docs/kickoffs/archive/` in this PR (unless an earlier PR of the item already moved it)
- [ ] UI PRs: screenshots at 1440 × 900 and 390 × 844 in `docs/screenshots/NNN/`, next to the matching `design/screenshots/*.png`

## Not tested

<!-- What was not tested, honestly. -->

## Deviations from the spec

<!-- Where the code and docs/SPEC.md disagree, and why. "None" if none. -->

## Design differences

<!-- Where the UI and design/ disagree, and why. "None" if none. -->
