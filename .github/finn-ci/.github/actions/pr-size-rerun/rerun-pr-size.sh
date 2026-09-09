#!/usr/bin/env bash
# Rerun the matching current-head pull-request workflow after it completes.
set -euo pipefail

repository="${1:?repository is required}"
current_run_id="${2:?current-run-id is required}"
pull_request_number="${3:?pull-request-number is required}"
max_attempts="${PR_SIZE_RERUN_MAX_ATTEMPTS:-80}"
poll_seconds="${PR_SIZE_RERUN_POLL_SECONDS:-3}"
: "${GH_TOKEN:?github-token is required}"

if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "repository must use owner/name form" >&2
  exit 1
fi
for value in "$current_run_id" "$pull_request_number" "$max_attempts"; do
  if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "run ids, pull-request-number, and max attempts must be positive integers" >&2
    exit 1
  fi
done
if [[ ! "$poll_seconds" =~ ^[0-9]+$ ]]; then
  echo "poll seconds must be a non-negative integer" >&2
  exit 1
fi

pull_request_json="$(
  gh api --method GET "repos/${repository}/pulls/${pull_request_number}"
)"
head_ref="$(jq -er '.head.ref | select(type == "string" and length > 0)' <<< "$pull_request_json")"
head_sha="$(jq -er '.head.sha | select(type == "string" and length > 0)' <<< "$pull_request_json")"
head_repository_id="$(jq -er '.head.repo.id | select(type == "number")' <<< "$pull_request_json")"
pull_request_run_name="PR Size / PR #${pull_request_number}"

current_run_json="$(
  gh api --method GET "repos/${repository}/actions/runs/${current_run_id}"
)"
workflow_id="$(jq -er '.workflow_id | select(type == "number")' <<< "$current_run_json")"

workflow_runs_json="$(
  gh api --method GET --paginate --slurp \
    -f "branch=${head_ref}" \
    -f "event=pull_request" \
    -f "per_page=100" \
    "repos/${repository}/actions/workflows/${workflow_id}/runs"
)"
# Match the PR's current head but deliberately allow an older recorded base.
# The rerun workflow fetches live PR state and measures the current base.
target_run_id="$(
  jq -er \
    --arg head_ref "$head_ref" \
    --arg head_sha "$head_sha" \
    --arg pull_request_run_name "$pull_request_run_name" \
    --argjson head_repository_id "$head_repository_id" \
    --argjson pull_number "$pull_request_number" '
    def current_head:
      .event == "pull_request" and
      .head_sha == $head_sha;
    def associated_with_pull_request:
      any(
        .pull_requests[]?;
        .number == $pull_number and
        .head.sha == $head_sha
      );
    def unassociated_fork_run:
      (.pull_requests | type == "array" and length == 0) and
      .head_branch == $head_ref and
      .head_repository.id == $head_repository_id and
      .display_title == $pull_request_run_name;

    [
      .[]?.workflow_runs[]?
      | select(current_head)
      | select(.id | type == "number")
    ]
    | [ .[] | select(associated_with_pull_request) ] as $associated
    | [ .[] | select(unassociated_fork_run) ] as $unassociated
    | if $associated | length > 0 then
        $associated | max_by(.id).id
      elif $unassociated | length > 0 then
        $unassociated | max_by(.id).id
      else
        empty
      end
  ' <<< "$workflow_runs_json"
)" || {
  echo "No PR size workflow run found for PR ${pull_request_number} at ${head_sha}." >&2
  exit 1
}

for ((attempt = 1; attempt <= max_attempts; attempt += 1)); do
  target_run_json="$(
    gh api --method GET "repos/${repository}/actions/runs/${target_run_id}"
  )"
  status="$(jq -er '.status | select(type == "string")' <<< "$target_run_json")"
  if [[ "$status" == "completed" ]]; then
    latest_pull_request_json="$(
      gh api --method GET "repos/${repository}/pulls/${pull_request_number}"
    )"
    latest_head_sha="$(
      jq -er '.head.sha | select(type == "string" and length > 0)' \
        <<< "$latest_pull_request_json"
    )"
    if [[ "$latest_head_sha" != "$head_sha" ]]; then
      echo "PR ${pull_request_number} head changed; refusing stale rerun." >&2
      exit 1
    fi
    gh api --method POST "repos/${repository}/actions/runs/${target_run_id}/rerun" >/dev/null
    echo "Rerunning PR size workflow ${target_run_id} for ${head_sha}."
    exit 0
  fi

  case "$status" in
    in_progress | pending | queued | requested | waiting) ;;
    *)
      echo "PR size workflow ${target_run_id} has unexpected status '${status}'." >&2
      exit 1
      ;;
  esac
  echo "Attempt ${attempt}: PR size workflow ${target_run_id} is ${status}."
  if ((attempt < max_attempts)); then
    sleep "$poll_seconds"
  fi
done

echo "PR size workflow ${target_run_id} did not finish before timeout." >&2
exit 1
