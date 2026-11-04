#!/usr/bin/env bash
# Work-item naming check (docs/work-items.md). Warnings only: never fails.
#
# Env:
#   PR_TITLE   the pull request title
#   PR_BRANCH  the head branch
#   PR_AUTHOR  the author's login (bots are skipped)
set -euo pipefail

case "${PR_AUTHOR:-}" in
  *"[bot]") echo "Bot PR: names not checked."; exit 0 ;;
esac

types='feat|fix|analysis|ops|spec'
title_re="^\[[0-9]{3}\] ($types): .+"
branch_re="^($types)/[0-9]{3}-[a-z0-9]+(-[a-z0-9]+)*$"

ok=1
if [[ ! $PR_TITLE =~ $title_re ]]; then
  echo "::warning::PR title should be '[NNN] type: Title' (docs/work-items.md), got: $PR_TITLE"
  ok=0
fi
if [[ ! $PR_BRANCH =~ $branch_re ]]; then
  echo "::warning::Branch should be 'type/NNN-slug' or 'spec/NNN-slug' (docs/work-items.md), got: $PR_BRANCH"
  ok=0
fi
# Title and branch should carry the same item number.
title_nr=$(grep -oE '^\[[0-9]{3}\]' <<<"$PR_TITLE" | tr -d '[]' || true)
branch_nr=$(grep -oE '/[0-9]{3}-' <<<"$PR_BRANCH" | tr -d '/-' || true)
if [[ -n $title_nr && -n $branch_nr && $title_nr != "$branch_nr" ]]; then
  echo "::warning::Title says item $title_nr, branch says item $branch_nr."
  ok=0
fi

{
  echo "## Work-item names"
  if ((ok)); then echo "Title and branch follow docs/work-items.md."
  else echo "Title or branch doesn't follow docs/work-items.md (see the warnings)."; fi
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
exit 0
