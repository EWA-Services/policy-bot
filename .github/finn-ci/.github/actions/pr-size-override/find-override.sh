#!/usr/bin/env bash
# Fetch current pull-request comments and emit only safe override metadata.
set -euo pipefail

repository="${1:?repository is required}"
pull_request_number="${2:?pull-request-number is required}"
: "${GH_TOKEN:?github-token is required}"

if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "repository must use owner/name form" >&2
  exit 1
fi
if [[ ! "$pull_request_number" =~ ^[1-9][0-9]*$ ]]; then
  echo "pull-request-number must be a positive integer" >&2
  exit 1
fi

action_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
gh api --method GET --paginate --slurp \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "repos/${repository}/issues/${pull_request_number}/comments?per_page=100" \
  | "$action_directory/evaluate-comments.py"
