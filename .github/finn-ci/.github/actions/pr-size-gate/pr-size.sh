#!/usr/bin/env bash
# Computes reviewable LOC and files for a pull-request diff.
set -euo pipefail

base_sha="${1:?base SHA is required}"
head_sha="${2:?head SHA is required}"
ignore_file="${PR_SIZE_IGNORE:-.pr-size-ignore}"

# Attribute rules can change binary detection and line counts. Use only the
# base revision's trusted policy so the pull request cannot classify text as
# binary and reduce its own reviewable size.
export GIT_ATTR_SOURCE="$base_sha"

if [[ -f "$ignore_file" ]]; then
  ignore_file="$(cd "$(dirname "$ignore_file")" && pwd)/$(basename "$ignore_file")"
else
  ignore_file="/dev/null"
fi

# Resolve exclusions outside the repository. This prevents its .gitignore and
# .git/info/exclude from silently changing the review policy.
ignore_directory="$(mktemp -d)"
trap 'rm -rf "$ignore_directory"' EXIT
git -C "$ignore_directory" init -q

is_magic_path_ignored() {
  local path="$1"
  local path_directory
  local probe_directory
  local status_file
  local status_record=""

  if ! probe_directory="$(mktemp -d "$ignore_directory/literal-path.XXXXXX")"; then
    return 2
  fi
  if ! git -C "$probe_directory" init -q; then
    rm -rf -- "$probe_directory"
    return 2
  fi
  path_directory="$(dirname -- "$path")"
  if ! mkdir -p -- "$probe_directory/$path_directory" ||
     ! : > "$probe_directory/$path"; then
    rm -rf -- "$probe_directory"
    return 2
  fi
  status_file="$probe_directory/.git/status"

  if ! env -u GIT_ATTR_SOURCE git --literal-pathspecs -C "$probe_directory" \
    -c core.excludesFile="$ignore_file" status --ignored --untracked-files=all \
    --porcelain=v1 -z -- "$path" > "$status_file"; then
    rm -rf -- "$probe_directory"
    return 2
  fi
  IFS= read -r -d '' status_record < "$status_file" || true
  rm -rf -- "$probe_directory"
  [[ "$status_record" == "!! "* ]]
}

is_ignored() {
  local path="$1"
  local result

  # check-ignore rejects a colon-prefixed path component even in literal mode.
  # Probe that rare case through a literal git-status pathspec instead.
  if [[ "/$path" == *"/:"* ]]; then
    is_magic_path_ignored "$path"
    return
  fi

  if printf '%s\0' "$path" | git -C "$ignore_directory" \
    -c core.excludesFile="$ignore_file" check-ignore --no-index --stdin -z -q; then
    return 0
  else
    result=$?
  fi
  if ((result == 1)); then
    return 1
  fi
  return 2
}

read_numstat() {
  local source_path="$1"
  local destination_path="$2"
  local ignore_whitespace="$3"
  local output=""
  local -a diff_arguments=(diff)

  if [[ "$ignore_whitespace" == "true" ]]; then
    diff_arguments+=(-w)
  fi
  diff_arguments+=(--numstat -M "$base_sha...$head_sha" -- "$source_path")
  if [[ "$destination_path" != "$source_path" ]]; then
    diff_arguments+=("$destination_path")
  fi

  # Diff paths come from the pull request. Treat pathspec-magic prefixes as
  # filename text so an author cannot make a changed path disappear from stats.
  if ! output="$(GIT_LITERAL_PATHSPECS=1 git "${diff_arguments[@]}")"; then
    return 1
  fi
  if [[ -z "$output" ]]; then
    numstat_added=0
    numstat_deleted=0
    return
  fi

  IFS=$'\t' read -r numstat_added numstat_deleted _ <<< "$output"
}

raw_loc=0
reviewable_loc=0
reviewable_files=0
name_status_file="$ignore_directory/name-status"
git diff --name-status -z -M "$base_sha...$head_sha" > "$name_status_file"

while IFS= read -r -d '' status; do
  IFS= read -r -d '' path
  source_path="$path"
  case "$status" in
    R* | C*) IFS= read -r -d '' path ;;
    A | D | M | T) ;;
    *)
      printf 'Unsupported git diff status: %s\n' "$status" >&2
      exit 1
      ;;
  esac

  # Raw LOC deliberately precedes policy and whitespace exclusions. Binary
  # changes have no text-line contribution.
  read_numstat "$source_path" "$path" false
  added="$numstat_added"
  deleted="$numstat_deleted"
  if [[ "$added" != "-" ]]; then
    raw_loc=$((raw_loc + added + deleted))
  fi

  ignore_result=0
  is_ignored "$path" || ignore_result=$?
  case "$ignore_result" in
    0) continue ;;
    1) ;;
    *)
      printf 'Could not evaluate PR-size ignore policy for %q.\n' "$path" >&2
      exit 1
      ;;
  esac

  # A removed file and a pure rename each cost a small, fixed review action.
  if [[ "$status" == "D" ]]; then
    reviewable_files=$((reviewable_files + 1))
    reviewable_loc=$((reviewable_loc + 1))
    continue
  fi

  if [[ "$added" == "-" ]]; then
    # Binary changes have no line count, but still take review attention.
    reviewable_files=$((reviewable_files + 1))
    continue
  fi
  if [[ "$added" == "0" && "$deleted" == "0" ]]; then
    reviewable_files=$((reviewable_files + 1))
    reviewable_loc=$((reviewable_loc + 1))
    continue
  fi

  # A whitespace-only formatter pass is not reviewable source change.
  read_numstat "$source_path" "$path" true
  if [[ "$numstat_added" == "0" && "$numstat_deleted" == "0" ]]; then
    continue
  fi

  reviewable_files=$((reviewable_files + 1))
  if (( added > deleted )); then
    reviewable_loc=$((reviewable_loc + added))
  else
    reviewable_loc=$((reviewable_loc + deleted))
  fi
done < "$name_status_file"

printf 'raw_loc=%s\n' "$raw_loc"
printf 'reviewable_loc=%s\n' "$reviewable_loc"
printf 'reviewable_files=%s\n' "$reviewable_files"
