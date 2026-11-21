#!/usr/bin/env bash
# Spec label and change-entry gate. Runs from .github/workflows/spec-label.yml.
#
# Env (set by the workflow):
#   BASE_SHA   base side of the PR diff
#   HEAD_SHA   PR head
#   HAS_LABEL  "true" when the PR carries the "spec change" label
#
# Output: a job summary listing the spec-owned files the diff touches,
# ::error:: per violated rule (fails the job), ::warning:: (never fails).
#
# Rules (AGENTS.md → How to work, docs/README.md rules 5 and 7):
#   1. A diff touching a path in .github/spec-paths.txt needs the label. (error)
#   2. The label on a PR that touches nothing spec-owned is stale. (warning)
#   3. Changes to docs/SPEC.md, the agent rules (AGENTS.md, CLAUDE.md), a
#      prompt or .env.example need a change entry: a changed item file in
#      docs/changes/ (from item 024) or docs/CHANGES.md (items up to 023).
#      Changes to design screens, design/DESIGN.md or design/tokens.css need
#      one in design/changes/ or design/CHANGES.md. A changes README is not
#      an entry. Other spec-owned files (docs/README.md, kickoffs, this gate)
#      need the label only. (error)
#   4. Archiving a kickoff is not a spec change: an unchanged move of
#      docs/kickoffs/<name>.md to docs/kickoffs/archive/<name>.md needs no
#      label. An edit in the same move, a move elsewhere or a move out of the
#      archive still does.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
paths_file=.github/spec-paths.txt
# A missing list must fail loudly: an empty list would let every change through.
test -f "$paths_file" || { echo "::error::$paths_file not found"; exit 1; }

mapfile -t patterns < <(grep -vE '^[[:space:]]*(#|$)' "$paths_file")
# Three dots: what the PR branch changed since the merge base. --no-renames
# reports a rename as delete + add, so a renamed spec file still counts.
mapfile -t changed < <(git diff --name-only --no-renames "$BASE_SHA...$HEAD_SHA")

# Rule 4: exact moves (git's R100) of a kickoff into docs/kickoffs/archive/,
# same file name. Both sides are exempt.
archived=()
while IFS=$'\t' read -r status old new; do
  [[ $status == R100 ]] || continue
  [[ $old == docs/kickoffs/*.md && $old != docs/kickoffs/archive/* ]] || continue
  [[ $new == "docs/kickoffs/archive/${old#docs/kickoffs/}" ]] || continue
  [[ ${old#docs/kickoffs/} != */* ]] || continue
  archived+=("$old" "$new")
done < <(git diff --name-status -M100% "$BASE_SHA...$HEAD_SHA")
is_archived() { printf '%s\n' "${archived[@]}" | grep -qxF "$1"; }

# Glob match: [[ == ]] lets '*' cross '/', so 'design/*' covers design/screens/x.
matches() {  # file, then patterns
  local f=$1; shift
  # shellcheck disable=SC2053  # unquoted on purpose: $p is a glob
  for p in "$@"; do [[ $f == $p ]] && return 0; done
  return 1
}
changed_has() { printf '%s\n' "${changed[@]}" | grep -qxF "$1"; }
# A changed item file <folder>/NNN-type-slug.md, not the folder's README.
changed_item_file() {
  local f
  for f in "${changed[@]}"; do
    [[ $f =~ ^$1/[0-9]{3}-(feat|fix|analysis|ops|spec)-[a-z0-9-]+\.md$ ]] && return 0
  done
  return 1
}
has_spec_entry() { changed_has docs/CHANGES.md || changed_item_file docs/changes; }
has_design_entry() { changed_has design/CHANGES.md || changed_item_file design/changes; }

spec_files=()
for f in "${changed[@]}"; do
  if ((${#archived[@]})) && is_archived "$f"; then continue; fi
  if matches "$f" "${patterns[@]}"; then spec_files+=("$f"); fi
done

{
  echo "## Spec label check"
  if ((${#spec_files[@]})); then
    echo "Spec files changed:"
    # shellcheck disable=SC2016  # literal backticks for Markdown
    printf -- '- `%s`\n' "${spec_files[@]}"
  else
    echo "No spec files changed."
  fi
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"

errors=0
# Rules 1 and 2.
if ((${#spec_files[@]})) && [[ $HAS_LABEL != true ]]; then
  echo "::error::This PR changes spec files but has no 'spec change' label: ${spec_files[*]}"
  errors=1
fi
if ((${#spec_files[@]} == 0)) && [[ $HAS_LABEL == true ]]; then
  echo "::warning::The 'spec change' label is set but no spec file changed. Remove the label."
fi

# Rule 3.
needs_spec_log=(docs/SPEC.md AGENTS.md CLAUDE.md 'backend/prompts/*' .env.example)
needs_design_log=('design/screens/*' design/DESIGN.md design/tokens.css)
for f in "${spec_files[@]}"; do
  if matches "$f" "${needs_spec_log[@]}" && ! has_spec_entry; then
    echo "::error file=$f::Spec change without a change entry: add the item's file in docs/changes/ (docs/changes/README.md)"
    errors=1
  fi
  if matches "$f" "${needs_design_log[@]}" && ! has_design_entry; then
    echo "::error file=$f::Design change without a design entry: add the item's file in design/changes/ (docs/changes/README.md)"
    errors=1
  fi
done

exit "$errors"
