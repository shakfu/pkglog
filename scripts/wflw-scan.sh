#!/usr/bin/env bash
# Scan CI status of every GitHub repo tracked in the pkgdb database.
# Emits one TSV line per (repo, workflow): repo, workflow, conclusion, branch, age, url
set -u
DB="${PKGDB:-$HOME/.pkgdb/pkg.db}"
LIMIT="${LIMIT:-40}"

repos=$(sqlite3 "$DB" \
  "select distinct repo_key from github_stats_history
   union select repo_key from github_cache order by 1;")

scan() {
  repo="$1"
  runs=$(gh run list -R "$repo" --limit "$LIMIT" \
      --json workflowName,conclusion,status,headBranch,createdAt,url,event 2>/dev/null) || {
    printf '%s\tERROR\tquery-failed\t-\t-\t-\n' "$repo"; return; }
  if [ -z "$runs" ] || [ "$runs" = "[]" ]; then
    printf '%s\t-\tNO_RUNS\t-\t-\t-\n' "$repo"; return
  fi
  # latest run per workflow name
  echo "$runs" | jq -r --arg repo "$repo" '
    group_by(.workflowName)
    | map(sort_by(.createdAt) | last)
    | .[]
    | [$repo, .workflowName,
       (if .status != "completed" then (.status|ascii_upcase) else (.conclusion|ascii_upcase) end),
       .headBranch, .createdAt, .url] | @tsv'
}

export -f scan
export LIMIT
printf '%s\n' "$repos" | xargs -P 8 -I{} bash -c 'scan "$@"' _ {}
