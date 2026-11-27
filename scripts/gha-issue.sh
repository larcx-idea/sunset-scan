#!/usr/bin/env bash
# Open, update or close the single Sunset Scan tracking issue. Called by action.yml.
# Inputs (env): GH_TOKEN, GITHUB_REPOSITORY, LABEL, TITLE, FINDINGS, REPORT.
set -euo pipefail
LABEL="${LABEL:-sunset-scan}"
TITLE="${TITLE:-Sunset Scan: things in this repo that stop working}"
FINDINGS="${FINDINGS:-0}"
repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"

existing=$(gh issue list --repo "$repo" --label "$LABEL" --state open --limit 1 \
  --json number --jq '.[0].number // empty' 2>/dev/null || true)

if [ "$FINDINGS" -gt 0 ]; then
  gh label create "$LABEL" --repo "$repo" --color F97316 \
    --description "Things that stop working on a date (sunset-scan)" >/dev/null 2>&1 || true
  if [ -n "$existing" ]; then
    gh issue edit "$existing" --repo "$repo" --body-file "$REPORT" >/dev/null
    echo "Updated issue #$existing"
  else
    gh issue create --repo "$repo" --title "$TITLE" --label "$LABEL" --body-file "$REPORT"
  fi
elif [ -n "$existing" ]; then
  gh issue close "$existing" --repo "$repo" \
    --comment "Sunset Scan no longer finds anything in this repo. Closing." >/dev/null
  echo "Closed issue #$existing"
else
  echo "Nothing to report; no issue opened."
fi
