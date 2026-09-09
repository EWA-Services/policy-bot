#!/usr/bin/env python3
"""GitHub API helper for finn-ai-coder CI checks and review metadata."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # pragma: no cover - direct workflow entrypoint
    from code_review_history import (
        REVIEW_INVOCATION_PATTERN,
        TRUSTED_LOGICAL_REVIEWER,
        TRUSTED_METADATA_AUTHOR,
        extract_metadata_envelope,
        trusted_reviews,
    )
else:
    from scripts.code_review_history import (
        REVIEW_INVOCATION_PATTERN,
        TRUSTED_LOGICAL_REVIEWER,
        TRUSTED_METADATA_AUTHOR,
        extract_metadata_envelope,
        trusted_reviews,
    )

CHECK_NAME = "finn-ai-coder / review"
METADATA_MARKER = "finn-ai-coder-review-metadata"
METADATA_AUTHOR_LOGIN = TRUSTED_METADATA_AUTHOR
LOGICAL_REVIEWER_LOGIN = TRUSTED_LOGICAL_REVIEWER
DIFF_SIGNATURE_ALGORITHM = "sha256:normalized-pr-diff-v1"
DEFAULT_API_VERSION = "2022-11-28"
_DIFF_INDEX_PREFIX = "index "
NORMALIZED_DIFF_IGNORED_PREFIXES = (
    _DIFF_INDEX_PREFIX,
    "similarity index ",
    "rename from ",
    "rename to ",
)
_DIFF_INDEX_PATTERN = re.compile(r"^index ([0-9a-fA-F]+)\.\.([0-9a-fA-F]+)(?: [0-7]{6})?$")


class GitHubApi:
    def __init__(
        self,
        token: str,
        *,
        api_url: str | None = None,
        auth_scheme: str = "Bearer",
        timeout: int = 60,
    ) -> None:
        self.token = token
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.auth_scheme = auth_scheme
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, Any]:
        url = path if path.startswith("https://") else f"{self.api_url}/{path.lstrip('/')}"
        request_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"{self.auth_scheme} {self.token}",
            "X-GitHub-Api-Version": os.environ.get("GITHUB_API_VERSION", DEFAULT_API_VERSION),
            "User-Agent": "finn-ai-coder-github-app",
        }
        if headers:
            request_headers.update(headers)
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read(), response.headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {method} {url} failed: HTTP {exc.code}: {body}") from exc

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | list[Any]:
        body, _ = self.request(method, path, payload=payload, headers=headers)
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def request_text(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> str:
        body, _ = self.request(method, path, headers=headers)
        return body.decode("utf-8", errors="replace")

    def paginate_list(self, path: str, *, item_key: str | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 101):
            page_path = f"{path}{separator}per_page=100&page={page}"
            data = self.request_json("GET", page_path)
            page_items: list[dict[str, Any]]
            if item_key:
                if not isinstance(data, dict):
                    raise RuntimeError(f"Expected object response for paginated {path}")
                page_items = list(data.get(item_key) or [])
            elif isinstance(data, list):
                page_items = list(data)
            else:
                raise RuntimeError(f"Expected list response for paginated {path}")
            items.extend(page_items)
            if len(page_items) < 100:
                break
        return items


def github_api_from_env(*, prefer_github_token: bool = False) -> GitHubApi:
    token_names = ("GITHUB_TOKEN", "GH_TOKEN") if prefer_github_token else ("GH_TOKEN", "GITHUB_TOKEN")
    token = next((os.environ.get(name) for name in token_names if os.environ.get(name)), "")
    if not token:
        raise RuntimeError("GH_TOKEN or GITHUB_TOKEN is required")
    return GitHubApi(token)


def _native_binary_identity(raw_lines: list[str]) -> str:
    index_lines = [line for line in raw_lines if line.startswith(_DIFF_INDEX_PREFIX)]
    match = _DIFF_INDEX_PATTERN.fullmatch(index_lines[0]) if len(index_lines) == 1 else None
    if match is None:
        raise RuntimeError("Native binary diff has no parseable source and target blob identity")
    source_blob = match.group(1).lower()
    target_blob = match.group(2).lower()
    binary_source = "created" if set(source_blob) == {"0"} else source_blob
    binary_target = "deleted" if set(target_blob) == {"0"} else target_blob
    return f"binary source blob {binary_source} target blob {binary_target}"


def _normalize_diff_block(raw_lines: list[str]) -> list[str]:
    native_binary = any(line.startswith("Binary files ") and line.endswith(" differ") for line in raw_lines)
    binary_identity = _native_binary_identity(raw_lines) if native_binary else ""

    normalized_lines: list[str] = []
    for raw_line in raw_lines:
        line = raw_line
        if line.startswith(_DIFF_INDEX_PREFIX):
            if native_binary:
                normalized_lines.append(binary_identity)
            continue
        if line.startswith(NORMALIZED_DIFF_IGNORED_PREFIXES):
            continue
        line = re.sub(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", "@@ @@", line)
        normalized_lines.append(line)
    return normalized_lines


def normalize_pr_diff(diff_text: str) -> str:
    raw_lines = diff_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[list[str]] = []
    for raw_line in raw_lines:
        if raw_line.startswith("diff --git ") and blocks and blocks[-1]:
            blocks.append([])
        if not blocks:
            blocks.append([])
        blocks[-1].append(raw_line)

    normalized_lines = [line for block in blocks for line in _normalize_diff_block(block)]
    return "\n".join(normalized_lines).strip("\n") + "\n"


def diff_signature(diff_text: str) -> str:
    normalized = normalize_pr_diff(diff_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_transient_diff_unavailable(error_message: str) -> bool:
    return "HTTP 500" in error_message and (
        '"code":"not_available"' in error_message or "diff is temporarily unavailable" in error_message
    )


def should_fallback_to_files_diff(error_message: str) -> bool:
    return "HTTP 406" in error_message or "diff exceeded the maximum number of files" in error_message


def fetch_pr_diff(api: GitHubApi, repo: str, pr_number: str) -> str:
    for attempt in range(1, 6):
        try:
            return api.request_text(
                "GET",
                f"repos/{repo}/pulls/{pr_number}",
                headers={"Accept": "application/vnd.github.v3.diff"},
            )
        except RuntimeError as exc:
            message = str(exc)
            if is_transient_diff_unavailable(message) and attempt < 5:
                delay = 2 ** (attempt - 1)
                print(
                    f"GitHub PR diff unavailable for {repo}#{pr_number}; retrying in {delay}s (attempt {attempt}/5).",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            if is_transient_diff_unavailable(message):
                raise RuntimeError(
                    f"GitHub PR diff remained unavailable for {repo}#{pr_number} "
                    "after 5 attempts; retry later instead of synthesizing a "
                    "potentially different diff signature."
                ) from exc
            if should_fallback_to_files_diff(message):
                return fetch_pr_files_diff(api, repo, pr_number)
            raise


def fetch_pr_files_diff(api: GitHubApi, repo: str, pr_number: str) -> str:
    pr = fetch_pr(api, repo, pr_number)
    files = api.paginate_list(f"repos/{repo}/pulls/{pr_number}/files")
    changed_files = int(pr.get("changed_files") or 0)
    if len(files) < changed_files:
        raise RuntimeError(
            f"GitHub files API returned {len(files)} of {changed_files} changed files "
            f"for {repo}#{pr_number}; refusing to synthesize an incomplete diff"
        )
    chunks: list[str] = []
    for file_info in files:
        chunks.extend(render_pr_file_diff(file_info, repo=repo, pr_number=pr_number))
    return "\n".join(chunks).strip("\n") + "\n"


def render_pr_file_diff(file_info: dict[str, Any], *, repo: str, pr_number: str) -> list[str]:
    filename = str(file_info.get("filename") or "")
    previous = str(file_info.get("previous_filename") or filename)
    status = str(file_info.get("status") or "modified")
    sha = str(file_info.get("sha") or "")
    patch = pr_file_patch(file_info, filename=filename, repo=repo, pr_number=pr_number, status=status, sha=sha)
    lines = [
        f"diff --git a/{previous} b/{filename}",
        pr_file_source_header(status, previous),
        pr_file_target_header(status, filename),
        patch,
    ]
    if patch == "## no text patch available":
        lines.insert(1, pr_file_metadata_line(file_info, status=status, sha=sha))
    return lines


def pr_file_metadata_line(file_info: dict[str, Any], *, status: str, sha: str) -> str:
    additions = int(file_info.get("additions") or 0)
    deletions = int(file_info.get("deletions") or 0)
    changes = int(file_info.get("changes") or (additions + deletions))
    return f"## file metadata: status={status} additions={additions} deletions={deletions} changes={changes} sha={sha}"


def pr_file_source_header(status: str, previous: str) -> str:
    return "--- /dev/null" if status == "added" else f"--- a/{previous}"


def pr_file_target_header(status: str, filename: str) -> str:
    return "+++ /dev/null" if status == "removed" else f"+++ b/{filename}"


def pr_file_patch(
    file_info: dict[str, Any],
    *,
    filename: str,
    repo: str,
    pr_number: str,
    status: str,
    sha: str,
) -> str:
    patch = str(file_info.get("patch") or "").strip("\n")
    if patch:
        return patch
    if not sha:
        raise RuntimeError(
            f"GitHub files API omitted both patch and sha for {filename} "
            f"in {repo}#{pr_number}; refusing to synthesize an unverifiable diff"
        )
    if status != "added":
        raise RuntimeError(
            f"GitHub files API omitted the patch and source blob identity for {filename} "
            f"in {repo}#{pr_number}; refusing to synthesize an unverifiable diff"
        )
    return "## no text patch available"


def fetch_pr(api: GitHubApi, repo: str, pr_number: str) -> dict[str, Any]:
    pr = api.request_json("GET", f"repos/{repo}/pulls/{pr_number}")
    if not isinstance(pr, dict):
        raise RuntimeError(f"Unexpected pull request response for {repo}#{pr_number}")
    return pr


def metadata_gate_runs_path(repo: str, head_ref: str) -> str:
    query = urllib.parse.urlencode(
        {
            "branch": head_ref,
            "event": "pull_request",
            "per_page": 100,
        }
    )
    return f"repos/{repo}/actions/workflows/pr-metadata-gate.yaml/runs?{query}"


def workflow_run_targets_pull_request(run: dict[str, Any], *, head_sha: str, pr_number: str) -> bool:
    if str(run.get("head_sha") or "") != head_sha:
        return False
    return any(
        str(candidate.get("number") or "") == str(pr_number)
        for candidate in run.get("pull_requests") or []
        if isinstance(candidate, dict)
    )


def metadata_gate_run_from_response(
    response: dict[str, Any] | list[Any],
    *,
    repo: str,
    pr_number: str,
    head_sha: str,
) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        raise RuntimeError(f"Unexpected workflow runs response for {repo}#{pr_number}")
    return next(
        (
            run
            for run in response.get("workflow_runs") or []
            if isinstance(run, dict) and workflow_run_targets_pull_request(run, head_sha=head_sha, pr_number=pr_number)
        ),
        None,
    )


def wait_for_metadata_gate_run(
    api: GitHubApi,
    *,
    workflow_runs_path: str,
    repo: str,
    pr_number: str,
    head_sha: str,
    max_poll_attempts: int,
    poll_interval_seconds: float,
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    target: dict[str, Any] | None = None
    for attempt in range(1, max_poll_attempts + 1):
        response = api.request_json("GET", workflow_runs_path)
        target = metadata_gate_run_from_response(
            response,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
        )
        if target is not None and str(target.get("status") or "") == "completed":
            return target
        if attempt < max_poll_attempts:
            sleeper(poll_interval_seconds)

    if target is None:
        raise RuntimeError(f"No PR Metadata Gate workflow run found for {repo}#{pr_number} at {head_sha}")
    raise RuntimeError(
        f"PR Metadata Gate workflow run for {repo}#{pr_number} did not complete after {max_poll_attempts} attempts"
    )


def rerun_metadata_gate(
    api: GitHubApi,
    *,
    repo: str,
    pr_number: str,
    max_poll_attempts: int = 80,
    poll_interval_seconds: float = 3,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Rerun the completed PR metadata workflow for the current pull request head."""
    if max_poll_attempts <= 0:
        raise ValueError("max_poll_attempts must be positive")
    pull_request = fetch_pr(api, repo, pr_number)
    head = pull_request.get("head") or {}
    head_sha = str(head.get("sha") or "").strip()
    head_ref = str(head.get("ref") or "").strip()
    if not head_sha or not head_ref:
        raise RuntimeError(f"Pull request {repo}#{pr_number} has no head SHA or ref")

    target = wait_for_metadata_gate_run(
        api,
        workflow_runs_path=metadata_gate_runs_path(repo, head_ref),
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        max_poll_attempts=max_poll_attempts,
        poll_interval_seconds=poll_interval_seconds,
        sleeper=sleeper,
    )

    run_id = int(target.get("id") or 0)
    if run_id <= 0:
        raise RuntimeError(f"PR Metadata Gate workflow run for {repo}#{pr_number} has no valid id")
    api.request_json("POST", f"repos/{repo}/actions/runs/{run_id}/rerun")
    return run_id


def output_values(values: dict[str, Any]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            rendered = "" if value is None else str(value)
            if "\n" in rendered:
                delimiter = "__FINN_AI_CODER_EOF__"
                while delimiter in rendered:
                    delimiter += "_X"
                handle.write(f"{key}<<{delimiter}\n{rendered}\n{delimiter}\n")
            else:
                handle.write(f"{key}={rendered}\n")


def check_run_payload(
    *,
    name: str,
    sha: str | None,
    status: str,
    conclusion: str | None,
    title: str,
    summary: str,
    details_url: str,
    external_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "output": {
            "title": title,
            "summary": summary,
        },
    }
    if sha:
        payload["name"] = name
        payload["head_sha"] = sha
    if conclusion:
        payload["conclusion"] = conclusion
    if details_url:
        payload["details_url"] = details_url
    if external_id:
        payload["external_id"] = external_id
    return payload


def check_run_sort_key(run: dict[str, Any]) -> tuple[str, int]:
    return run.get("started_at") or "", int(run.get("id") or 0)


def select_check_run_for_update(
    check_runs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select the newest active check; completed history is never rewritten."""
    return next((run for run in check_runs if run.get("status") in {"queued", "in_progress"}), None)


def check_runs_path(repo: str, sha: str, name: str) -> str:
    query = urllib.parse.urlencode({"check_name": name, "filter": "all"})
    return f"repos/{repo}/commits/{sha}/check-runs?{query}"


def upsert_check_run(
    api: GitHubApi,
    *,
    repo: str,
    sha: str,
    name: str,
    status: str,
    conclusion: str | None,
    title: str,
    summary: str,
    details_url: str = "",
    external_id: str = "",
) -> dict[str, Any]:
    if status != "completed" and conclusion:
        raise RuntimeError("Check run conclusion may only be set when status is completed")
    check_runs = api.paginate_list(
        check_runs_path(repo, sha, name),
        item_key="check_runs",
    )
    matching = [run for run in check_runs if run.get("name") == name]
    matching.sort(key=check_run_sort_key, reverse=True)
    check_to_update = select_check_run_for_update(matching)

    payload = check_run_payload(
        name=name,
        sha=sha if check_to_update is None else None,
        status=status,
        conclusion=conclusion,
        title=title,
        summary=summary,
        details_url=details_url,
        external_id=external_id,
    )
    if check_to_update is not None:
        updated = api.request_json("PATCH", f"repos/{repo}/check-runs/{check_to_update['id']}", payload=payload)
        if not isinstance(updated, dict):
            raise RuntimeError("Unexpected check-run update response")
        updated["upsert_action"] = "updated"
        return updated

    created = api.request_json("POST", f"repos/{repo}/check-runs", payload=payload)
    if not isinstance(created, dict):
        raise RuntimeError("Unexpected check-run create response")
    created["upsert_action"] = "created"
    return created


def metadata_comment_body(payload: dict[str, Any]) -> str:
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"<!-- {METADATA_MARKER}\n{rendered}\n-->\n"


def metadata_review_event(metadata: dict[str, Any]) -> str:
    verdict = normalize_verdict(str(metadata.get("verdict") or "none"))
    if verdict == "approved":
        return "APPROVE"
    if verdict == "requested_changes":
        return "REQUEST_CHANGES"
    return "COMMENT"


def metadata_review_body(metadata: dict[str, Any]) -> str:
    body = metadata_comment_body(metadata)
    verdict = normalize_verdict(str(metadata.get("verdict") or "none"))
    if verdict not in {"approved", "requested_changes"}:
        return body

    label = "APPROVE" if verdict == "approved" else "REQUEST_CHANGES"
    provider = str(metadata.get("provider") or "unknown")
    reason = str(metadata.get("verdict_reason") or "(no reason provided)")
    return f"AI review (finn-ai-coder): verdict={label}.\n\nProvider: {provider}\nReason: {reason}\n\n{body}"


def extract_metadata(body: str) -> dict[str, Any] | None:
    return extract_metadata_envelope(body)


def metadata_diff_value(metadata: dict[str, Any]) -> str:
    diff = metadata.get("diff_signature") or {}
    return str(diff.get("value") or "")


def metadata_normalized_verdict(metadata: dict[str, Any]) -> str:
    try:
        return normalize_verdict(str(metadata.get("verdict") or ""))
    except RuntimeError:
        return ""


def normalize_verdict(verdict: str) -> str:
    value = verdict.strip().lower().replace("-", "_")
    aliases = {
        "approve": "approved",
        "approved": "approved",
        "success": "approved",
        "request_changes": "requested_changes",
        "requested_changes": "requested_changes",
        "changes_requested": "requested_changes",
        "failure": "requested_changes",
        "blocker": "requested_changes",
        "blocked": "requested_changes",
        "none": "none",
        "no_review": "none",
        "error": "error",
        "internal_error": "error",
    }
    if value not in aliases:
        raise RuntimeError(f"Unsupported verdict: {verdict}")
    return aliases[value]


def select_applicable_metadata(
    reviews: list[dict[str, Any]],
    current_diff_signature: str,
    *,
    current_head_sha: str,
    repo: str,
    pr_number: int,
) -> dict[str, Any] | None:
    history = trusted_reviews(
        reviews,
        reviewers=[LOGICAL_REVIEWER_LOGIN],
        repo=repo,
        pr_number=pr_number,
        include_metadata=True,
    )
    for trusted in reversed(history):
        metadata = trusted["metadata"]
        if metadata_diff_value(metadata) != current_diff_signature:
            continue
        if metadata.get("provider") == "human-override" and metadata.get("head_sha") != current_head_sha:
            continue
        return metadata
    return None


def pr_coordinates(pull_request: dict[str, Any]) -> tuple[str, str, bool]:
    head_sha = str((pull_request.get("head") or {}).get("sha") or "").strip()
    base_sha = str((pull_request.get("base") or {}).get("sha") or "").strip()
    if not head_sha:
        raise RuntimeError("Pull request response has no head SHA")
    labels = {
        str(label.get("name") if isinstance(label, dict) else label).casefold()
        for label in pull_request.get("labels") or []
    }
    return head_sha, base_sha, "ai-review-breaker" in labels


def default_run_url(repo: str) -> str:
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"{server_url}/{repo}/actions/runs/{run_id}" if run_id else ""


def build_review_metadata(
    *,
    repo: str,
    pr_number: str,
    provider: str,
    verdict: str,
    head_sha: str,
    signature: str,
    reason: str,
    review_invocation_id: str = "",
    timestamp: str | None = None,
) -> dict[str, Any]:
    metadata = {
        "schema": "finn-ai-coder-review-metadata/v1",
        "check_name": CHECK_NAME,
        "repository": repo,
        "pull_request": int(pr_number),
        "provider": provider,
        "verdict": normalize_verdict(verdict),
        "verdict_reason": reason,
        "head_sha": head_sha,
        "diff_signature": {
            "algorithm": DIFF_SIGNATURE_ALGORITHM,
            "value": signature,
        },
        "timestamp": timestamp
        or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    if review_invocation_id:
        if not REVIEW_INVOCATION_PATTERN.fullmatch(review_invocation_id):
            raise ValueError("Review invocation ID must be a canonical positive decimal integer")
        metadata["review_invocation_id"] = review_invocation_id
    return metadata


def create_metadata_review(
    api: GitHubApi,
    *,
    repo: str,
    pr_number: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "body": metadata_review_body(metadata),
        "event": metadata_review_event(metadata),
    }
    head_sha = str(metadata.get("head_sha") or "").strip()
    if head_sha:
        payload["commit_id"] = head_sha

    created = api.request_json(
        "POST",
        f"repos/{repo}/pulls/{pr_number}/reviews",
        payload=payload,
    )
    if not isinstance(created, dict):
        raise RuntimeError("Unexpected pull request review create response")
    return created


@dataclass(frozen=True)
class ReviewTarget:
    head_sha: str
    base_sha: str
    signature: str
    breaker_active: bool


@dataclass(frozen=True)
class PublicationResult:
    action: str
    head_sha: str
    signature: str
    metadata_recorded: bool = False
    check_run_id: int | str = ""
    check_published: bool = False
    evaluate_breaker: bool = False


def read_review_target(read_api: GitHubApi, *, repo: str, pr_number: str) -> ReviewTarget:
    """Read one normalized diff tied to an unchanged PR head and base."""
    before = fetch_pr(read_api, repo, pr_number)
    before_head, before_base, _ = pr_coordinates(before)
    if not before_base:
        raise RuntimeError("Pull request response has no base SHA")

    signature = diff_signature(fetch_pr_diff(read_api, repo, pr_number))
    after = fetch_pr(read_api, repo, pr_number)
    after_head, after_base, _ = pr_coordinates(after)
    if after_head != before_head:
        raise RuntimeError("Pull request head changed while reading the review result target")
    if after_base != before_base:
        raise RuntimeError("Pull request base changed while reading the review result target")
    if __package__ in {None, ""}:  # pragma: no cover - exercised through the direct-script subprocess test
        from code_review_breaker import breaker_is_active
    else:
        from scripts.code_review_breaker import breaker_is_active

    breaker_active = breaker_is_active(
        labels=list(after.get("labels") or []),
        events=read_api.paginate_list(f"repos/{repo}/issues/{pr_number}/events"),
        issue_comments=read_api.paginate_list(f"repos/{repo}/issues/{pr_number}/comments"),
        repo=repo,
        pr_number=int(pr_number),
    )
    return ReviewTarget(after_head, after_base, signature, breaker_active)


def publish_review_result(
    read_api: GitHubApi,  # NOSONAR - Explicit publication data and injectable side-effect seams stay separate.
    metadata_api: GitHubApi,
    check_api: GitHubApi,
    *,
    repo: str,
    pr_number: str,
    expected_head: str,
    expected_signature: str,
    provider: str,
    verdict: str,
    reason: str,
    title: str,
    summary: str,
    model: str = "",
    review_invocation_id: str = "",
    details_url: str = "",
    external_id: str = "",
    target_reader: Callable[..., ReviewTarget] = read_review_target,
    metadata_writer: Callable[..., dict[str, Any]] = create_metadata_review,
    check_writer: Callable[..., dict[str, Any]] = upsert_check_run,
    warning_writer: Callable[[str], None] = lambda message: print(f"::warning::{message}"),
) -> PublicationResult:
    """Publish one durable result, then attempt one terminal check projection."""
    target = target_reader(read_api, repo=repo, pr_number=pr_number)
    if target.head_sha != expected_head or target.signature != expected_signature:
        return PublicationResult("stale", target.head_sha, target.signature)

    normalized_verdict = normalize_verdict(verdict)
    metadata = build_review_metadata(
        repo=repo,
        pr_number=pr_number,
        provider=provider,
        verdict=normalized_verdict,
        head_sha=target.head_sha,
        signature=target.signature,
        reason=reason,
        review_invocation_id=review_invocation_id,
    )
    metadata_writer(metadata_api, repo=repo, pr_number=pr_number, metadata=metadata)

    conclusion = "success" if normalized_verdict == "approved" else "failure"
    check_summary = f"{summary}\n\nModel: {model}" if model else summary
    try:
        check = check_writer(
            check_api,
            repo=repo,
            sha=target.head_sha,
            name=CHECK_NAME,
            status="completed",
            conclusion=conclusion,
            title=title,
            summary=check_summary,
            details_url=details_url or default_run_url(repo),
            external_id=external_id,
        )
    except Exception:
        warning_writer("Could not publish the terminal finn-ai-coder review check.")
        check = {}
    return PublicationResult(
        action="published",
        head_sha=target.head_sha,
        signature=target.signature,
        metadata_recorded=True,
        check_run_id=check.get("id", ""),
        check_published=bool(check),
        evaluate_breaker=(provider != "human-override" and normalized_verdict in {"approved", "requested_changes"}),
    )


def refresh_current_check(
    read_api: GitHubApi,
    write_api: GitHubApi,
    *,
    repo: str,
    pr_number: str,
    name: str,
    details_url: str = "",
    external_id: str = "",
    target_reader: Callable[..., ReviewTarget] = read_review_target,
    check_writer: Callable[..., dict[str, Any]] = upsert_check_run,
) -> tuple[ReviewTarget, str, str, str | None, dict[str, Any]]:
    """Read the current durable result once and attempt one check projection."""
    target = target_reader(read_api, repo=repo, pr_number=pr_number)
    reviews = read_api.paginate_list(f"repos/{repo}/pulls/{pr_number}/reviews")
    applicable = select_applicable_metadata(
        reviews,
        target.signature,
        current_head_sha=target.head_sha,
        repo=repo,
        pr_number=int(pr_number),
    )
    if target.breaker_active and applicable is None:
        verdict = "breaker"
        status = "completed"
        conclusion = "action_required"
        title = "AI review paused"
        summary = "AI review is paused by the ai-review-breaker label."
    elif applicable is None:
        verdict = "none"
        status = "queued"
        conclusion = None
        title = "AI review queued"
        summary = "The current normalized PR diff is waiting for finn-ai-coder review."
    else:
        verdict = normalize_verdict(str(applicable.get("verdict") or "none"))
        status = "completed"
        conclusion = "success" if verdict == "approved" else "failure"
        title = {
            "approved": "AI review approved",
            "requested_changes": "AI review requested changes",
            "error": "AI review failed",
            "none": "AI review required",
        }[verdict]
        summary = str(applicable.get("verdict_reason") or title)

    check = check_writer(
        write_api,
        repo=repo,
        sha=target.head_sha,
        name=name,
        status=status,
        conclusion=conclusion,
        title=title,
        summary=summary,
        details_url=details_url or default_run_url(repo),
        external_id=external_id,
    )
    return target, verdict, status, conclusion, check


def command_resolve_target(args: argparse.Namespace) -> int:
    read_api = github_api_from_env(prefer_github_token=True)
    pr = fetch_pr(read_api, args.repo, args.pr_number)
    head_sha = str((pr.get("head") or {}).get("sha") or "").strip()
    base_sha = str((pr.get("base") or {}).get("sha") or "").strip()
    if not head_sha:
        raise RuntimeError(f"Pull request {args.repo}#{args.pr_number} has no head SHA")
    if not base_sha:
        raise RuntimeError(f"Pull request {args.repo}#{args.pr_number} has no base SHA")
    signature = diff_signature(fetch_pr_diff(read_api, args.repo, args.pr_number))
    output_values(
        {
            "head_sha": head_sha,
            "base_sha": base_sha,
            "diff_signature": signature,
        }
    )
    print(f"Resolved review target for {args.repo}#{args.pr_number} at {head_sha}")
    return 0


def command_publish_result(args: argparse.Namespace) -> int:
    wrapper_token = os.environ.get("GITHUB_TOKEN")
    app_token = os.environ.get("GH_TOKEN")
    if not wrapper_token:
        raise RuntimeError("GITHUB_TOKEN is required to publish durable review metadata")
    if not app_token:
        raise RuntimeError("GH_TOKEN is required to publish the review check")
    result = publish_review_result(
        GitHubApi(wrapper_token),
        GitHubApi(wrapper_token),
        GitHubApi(app_token),
        repo=args.repo,
        pr_number=args.pr_number,
        expected_head=args.expected_head,
        expected_signature=args.expected_signature,
        provider=args.provider,
        verdict=args.verdict,
        reason=args.reason,
        title=args.title,
        summary=args.summary,
        model=args.model,
        review_invocation_id=args.review_invocation_id,
        details_url=args.details_url,
        external_id=args.external_id,
    )
    output_values(
        {
            "action": result.action,
            "head_sha": result.head_sha,
            "diff_signature": result.signature,
            "metadata_recorded": str(result.metadata_recorded).lower(),
            "check_run_id": result.check_run_id,
            "check_published": str(result.check_published).lower(),
            "evaluate_breaker": str(result.evaluate_breaker).lower(),
        }
    )
    print(f"Review result action for {args.repo}#{args.pr_number}: {result.action}")
    return 0


def command_refresh_check(args: argparse.Namespace) -> int:
    target, verdict, status, conclusion, check = refresh_current_check(
        github_api_from_env(prefer_github_token=True),
        github_api_from_env(),
        repo=args.repo,
        pr_number=args.pr_number,
        name=args.name,
        details_url=args.details_url,
        external_id=args.external_id,
    )
    output_values(
        {
            "ai_review_verdict": verdict,
            "diff_signature": target.signature,
            "head_sha": target.head_sha,
            "check_run_id": check.get("id", ""),
            "check_run_url": check.get("html_url") or check.get("url") or "",
            "check_run_status": status,
            "check_run_conclusion": conclusion or "",
        }
    )
    print(f"Refreshed {args.name} on {target.head_sha}: {status} ({verdict})")
    return 0


def command_rerun_metadata_gate(args: argparse.Namespace) -> int:
    wrapper_token = os.environ.get("GITHUB_TOKEN")
    if not wrapper_token:
        raise RuntimeError("GITHUB_TOKEN is required to rerun the PR Metadata Gate")
    run_id = rerun_metadata_gate(
        GitHubApi(wrapper_token),
        repo=args.repo,
        pr_number=args.pr_number,
    )
    output_values({"metadata_gate_run_id": run_id})
    print(f"Triggered PR Metadata Gate rerun {run_id} for {args.repo}#{args.pr_number}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="finn-ai-coder GitHub App CI helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    target = subparsers.add_parser("resolve-target")
    target.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), required=False)
    target.add_argument("--pr-number", required=True)
    target.set_defaults(func=command_resolve_target)

    publish = subparsers.add_parser("publish-result")
    publish.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), required=False)
    publish.add_argument("--pr-number", required=True)
    publish.add_argument("--expected-head", required=True)
    publish.add_argument("--expected-signature", required=True)
    publish.add_argument("--provider", required=True)
    publish.add_argument("--verdict", required=True)
    publish.add_argument("--reason", default="")
    publish.add_argument("--title", required=True)
    publish.add_argument("--summary", required=True)
    publish.add_argument("--model", default="")
    publish.add_argument("--review-invocation-id", default="")
    publish.add_argument("--details-url", default="")
    publish.add_argument("--external-id", default="")
    publish.set_defaults(func=command_publish_result)

    refresh = subparsers.add_parser("refresh-check")
    refresh.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), required=False)
    refresh.add_argument("--pr-number", required=True)
    refresh.add_argument("--name", default=CHECK_NAME)
    refresh.add_argument("--details-url", default="")
    refresh.add_argument("--external-id", default="")
    refresh.set_defaults(func=command_refresh_check)

    rerun = subparsers.add_parser("rerun-metadata-gate")
    rerun.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), required=False)
    rerun.add_argument("--pr-number", required=True)
    rerun.set_defaults(func=command_rerun_metadata_gate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "repo") and not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
