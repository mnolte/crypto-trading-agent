# Kickoff messages for Devin

Paste one message per PR. Change only the step number and name.

## First session (PR 1)

```
We are building Trading Agent v0.1, a local paper-trading crypto agent (Python FastAPI
backend, React frontend). Everything you need is in the repo:

- docs/README.md: how to work with the spec (read first)
- docs/SPEC.md: the build spec, source of truth for behaviour
- design/DESIGN.md: the visual reference, source of truth for look and wording
- backend/prompts/decision.v1.md: the model's system prompt (do not edit)

Build PR 1 of the build order: "Skeleton". Read the sections docs/README.md lists for
PR 1. Also build every shared UI component on a hidden /dev/components page, as
design/DESIGN.md describes.

Do not start PR 2. If the spec is silent or ambiguous on anything that changes
behaviour, ask me instead of guessing. Use the PR template in docs/README.md.
```

## Every later PR

```
Next: PR <N> of the build order, "<step name>". Read the sections docs/README.md lists
for PR <N>, plus any CHANGES.md entries since your last PR. Build only this step, with
its tests passing. UI PRs attach Playwright screenshots at 1440 and 1100 px next to the
matching design/screenshots/*.png. Ask before guessing on anything that changes
behaviour. Use the PR template in docs/README.md.
```
