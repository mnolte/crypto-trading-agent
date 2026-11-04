#!/usr/bin/env bash
# Spec label and changelog check. Runs from .github/workflows/spec-label.yml.
#
# Env:
#   BASE_SHA   base side of the PR diff
#   HEAD_SHA   PR head
#   HAS_LABEL  "true" when the PR has the "spec-change" label
#
# Rules (docs/README.md "When the spec changes", CLAUDE.md):
#   1. A diff that touches a path in .github/spec-paths.txt needs the label. (error)
#   2. The label on a PR that touches no spec-owned path is stale. (warning)
#   3. docs/SPEC.md, AGENTS.md, a prompt or .env.example changed → an entry in
#      docs/CHANGES.md; design/screens, design/DESIGN.md or design/tokens.css
#      changed → an entry in design/CHANGES.md. (error)
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
paths_file=.github/spec-paths.txt
# A missing list must fail loudly: an empty list would let every change through.
test -f "$paths_file" || { echo "::error::$paths_file not found"; exit 1; }

mapfile -t patterns < <(grep -vE '^[[:space:]]*(#|$)' "$paths_file")
# Three dots: what the PR branch changed since the merge base. --no-renames
# reports a rename as delete + add, so moving a spec file still counts.
mapfile -t changed < <(git diff --name-only --no-renames "$BASE_SHA...$HEAD_SHA")

matches() {  # file, then patterns
  local f=$1; shift
  local p
  for p in "$@"; do
    # shellcheck disable=SC2053  # the pattern is meant to glob
    [[ $f == $p ]] && return 0
  done
  return 1
}
changed_has() { printf '%s\n' "${changed[@]}" | grep -qxF "$1"; }

spec_files=()
for f in "${changed[@]}"; do
  if matches "$f" "${patterns[@]}"; then spec_files+=("$f"); fi
done

{
  echo "## Spec label check"
  if ((${#spec_files[@]})); then
    echo "Spec-owned files changed:"
    # shellcheck disable=SC2016  # literal backticks for Markdown
    printf -- '- `%s`\n' "${spec_files[@]}"
  else
    echo "No spec-owned files changed."
  fi
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"

errors=0
if ((${#spec_files[@]})) && [[ $HAS_LABEL != true ]]; then
  echo "::error::This PR changes spec-owned files but has no 'spec-change' label: ${spec_files[*]}"
  errors=1
fi
if ((${#spec_files[@]} == 0)) && [[ $HAS_LABEL == true ]]; then
  echo "::warning::The 'spec-change' label is set but no spec-owned file changed. Remove the label."
fi

needs_spec_log=(docs/SPEC.md AGENTS.md 'backend/prompts/*' .env.example)
needs_design_log=('design/screens/*' design/DESIGN.md design/tokens.css)
for f in "${spec_files[@]}"; do
  if matches "$f" "${needs_spec_log[@]}" && ! changed_has docs/CHANGES.md; then
    echo "::error file=$f::$f changed without an entry in docs/CHANGES.md"
    errors=1
  fi
  if matches "$f" "${needs_design_log[@]}" && ! changed_has design/CHANGES.md; then
    echo "::error file=$f::$f changed without an entry in design/CHANGES.md"
    errors=1
  fi
done

if ((errors == 0)); then echo "Spec label check passed."; fi
exit "$errors"
