#!/usr/bin/env bash
# Persist the single bot-owned PR-size marker comment.
set -euo pipefail

: "${GH_TOKEN:?github-token is required}"
: "${PR_SIZE_REPOSITORY:?repository is required}"
: "${PR_SIZE_PR_NUMBER:?pull-request-number is required}"
: "${PR_SIZE_MODE:?mode is required}"
: "${PR_SIZE_LOC:?reviewable-loc is required}"
: "${PR_SIZE_FILES:?reviewable-files is required}"
: "${PR_SIZE_WARNED:?warned is required}"
: "${PR_SIZE_BLOCKED:?blocked is required}"
override_actor="${PR_SIZE_OVERRIDE_ACTOR:-}"

if [[ ! "$PR_SIZE_REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "repository must use owner/name form" >&2
  exit 1
fi
if [[ ! "$PR_SIZE_PR_NUMBER" =~ ^[1-9][0-9]*$ ]]; then
  echo "pull-request-number must be a positive integer" >&2
  exit 1
fi

action_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
temporary_root="${RUNNER_TEMP:-.}"
temporary_directory="$(mktemp -d "$temporary_root/pr-size-comment.XXXXXX")"
trap 'rm -rf "$temporary_directory"' EXIT
comments_file="$temporary_directory/comments.json"
plan_file="$temporary_directory/plan.json"
request_file="$temporary_directory/request.json"

gh api --method GET --paginate --slurp \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "repos/${PR_SIZE_REPOSITORY}/issues/${PR_SIZE_PR_NUMBER}/comments?per_page=100" \
  > "$comments_file"

"$action_directory/plan-comment.py" \
  --mode "$PR_SIZE_MODE" \
  --reviewable-loc "$PR_SIZE_LOC" \
  --reviewable-files "$PR_SIZE_FILES" \
  --warned "$PR_SIZE_WARNED" \
  --blocked "$PR_SIZE_BLOCKED" \
  --override-actor "$override_actor" \
  < "$comments_file" > "$plan_file"

operation_count="$(jq -er '.operations | select(type == "array") | length' "$plan_file")"
for ((index = 0; index < operation_count; index += 1)); do
  operation_json="$(jq -cer --argjson index "$index" '.operations[$index]' "$plan_file")"
  operation="$(jq -r '.operation' <<< "$operation_json")"
  case "$operation" in
    create)
      jq '{body: .body}' <<< "$operation_json" > "$request_file"
      gh api --method POST \
        "repos/${PR_SIZE_REPOSITORY}/issues/${PR_SIZE_PR_NUMBER}/comments" \
        --input "$request_file" >/dev/null
      ;;
    update)
      comment_id="$(jq -er '.comment_id' <<< "$operation_json")"
      jq '{body: .body}' <<< "$operation_json" > "$request_file"
      gh api --method PATCH \
        "repos/${PR_SIZE_REPOSITORY}/issues/comments/${comment_id}" \
        --input "$request_file" >/dev/null
      ;;
    delete)
      comment_id="$(jq -er '.comment_id' <<< "$operation_json")"
      gh api --method DELETE \
        "repos/${PR_SIZE_REPOSITORY}/issues/comments/${comment_id}" >/dev/null
      ;;
    *)
      echo "Unknown PR-size comment operation: $operation" >&2
      exit 1
      ;;
  esac
done
