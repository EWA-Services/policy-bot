#!/usr/bin/env python3
"""Collect strictly trusted durable FINN AI review history."""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

METADATA_MARKER = "finn-ai-coder-review-metadata"
METADATA_OPENING = f"<!-- {METADATA_MARKER}\n"
METADATA_SCHEMA = "finn-ai-coder-review-metadata/v1"
TRUSTED_METADATA_AUTHOR = "github-actions[bot]"
TRUSTED_LOGICAL_REVIEWER = "finn-ai-coder[bot]"
DIFF_SIGNATURE_ALGORITHM = "sha256:normalized-pr-diff-v1"
CANONICAL_VERDICTS = frozenset({"approved", "requested_changes", "none", "error"})
METADATA_FUTURE_SKEW = dt.timedelta(minutes=5)
_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")
_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
REVIEW_INVOCATION_PATTERN = re.compile(r"^[1-9]\d*(?::[1-9]\d*)?$", re.ASCII)
_HISTORICAL_HEAD_RESOLUTION_ERROR = "Unable to resolve historical review heads"


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not _TIMESTAMP_PATTERN.fullmatch(value):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def valid_review_invocation_id(value: Any) -> bool:
    return value is None or isinstance(value, str) and REVIEW_INVOCATION_PATTERN.fullmatch(value) is not None


def extract_metadata_envelope(body: Any) -> dict[str, Any] | None:
    """Extract one canonical metadata envelope from the end of a review body."""
    if not isinstance(body, str):
        return None
    stripped = body.rstrip()
    if stripped.count(METADATA_OPENING) != 1:
        return None
    start = stripped.find(METADATA_OPENING)
    end = stripped.rfind("-->")
    if end < start or end + len("-->") != len(stripped):
        return None
    encoded = stripped[start + len(METADATA_OPENING) : end]
    if not encoded.endswith("\n"):
        return None
    try:
        metadata = json.loads(encoded[:-1])
    except json.JSONDecodeError:
        return None
    return metadata if isinstance(metadata, dict) else None


def parse_trusted_metadata(
    metadata: dict[str, Any],
    *,
    reviewers: list[str],
    repo: str,
    pr_number: int,
) -> dict[str, Any] | None:
    """Return normalized metadata only when every durable trust invariant holds."""
    if metadata.get("schema") != METADATA_SCHEMA or metadata.get("repository") != repo:
        return None
    configured_reviewers = {login.casefold() for login in reviewers}
    if TRUSTED_LOGICAL_REVIEWER.casefold() not in configured_reviewers:
        return None
    metadata_pr = metadata.get("pull_request")
    if isinstance(metadata_pr, bool) or not isinstance(metadata_pr, int) or metadata_pr != pr_number:
        return None
    verdict = metadata.get("verdict")
    if not isinstance(verdict, str) or verdict not in CANONICAL_VERDICTS:
        return None
    timestamp = metadata.get("timestamp")
    if parse_timestamp(timestamp) is None:
        return None

    diff_signature = metadata.get("diff_signature")
    if not isinstance(diff_signature, dict):
        return None
    if diff_signature.get("algorithm") != DIFF_SIGNATURE_ALGORITHM:
        return None
    signature = diff_signature.get("value")
    if not isinstance(signature, str) or not _SIGNATURE_PATTERN.fullmatch(signature):
        return None

    head_sha = metadata.get("head_sha")
    if not isinstance(head_sha, str) or not _COMMIT_PATTERN.fullmatch(head_sha):
        return None

    review_invocation_id = metadata.get("review_invocation_id")
    if not valid_review_invocation_id(review_invocation_id):
        return None

    return {
        "timestamp": timestamp,
        "verdict": verdict,
        "head_sha": head_sha.lower(),
        "review_invocation_id": review_invocation_id,
        "diff_signature": {
            "algorithm": DIFF_SIGNATURE_ALGORITHM,
            "value": signature,
        },
    }


def parse_trusted_review(
    review: dict[str, Any],
    *,
    reviewers: list[str],
    repo: str,
    pr_number: int,
    include_metadata: bool = False,
) -> dict[str, Any] | None:
    """Return a normalized durable review only when every trust invariant holds."""
    user = review.get("user")
    if not isinstance(user, dict) or user.get("type") != "Bot":
        return None
    metadata_author = user.get("login")
    if not isinstance(metadata_author, str) or metadata_author.casefold() != TRUSTED_METADATA_AUTHOR.casefold():
        return None

    metadata = extract_metadata_envelope(review.get("body"))
    if metadata is None:
        return None
    trusted_metadata = parse_trusted_metadata(
        metadata,
        reviewers=reviewers,
        repo=repo,
        pr_number=pr_number,
    )
    if trusted_metadata is None:
        return None
    submitted_at = review.get("submitted_at")
    submitted_time = parse_timestamp(submitted_at)
    metadata_time = parse_timestamp(trusted_metadata["timestamp"])
    if submitted_time is None or metadata_time is None:
        return None
    if metadata_time > submitted_time + METADATA_FUTURE_SKEW:
        return None
    allowed_states = {
        "approved": {"APPROVED", "DISMISSED"},
        "requested_changes": {"CHANGES_REQUESTED", "DISMISSED"},
        "none": {"COMMENTED"},
        "error": {"COMMENTED"},
    }
    if review.get("state") not in allowed_states[trusted_metadata["verdict"]]:
        return None
    review_id = review.get("id")
    if isinstance(review_id, bool) or not isinstance(review_id, int) or review_id <= 0:
        return None

    parsed = {
        "review_id": review_id,
        "review_url": str(review.get("html_url") or ""),
        "reviewer": TRUSTED_LOGICAL_REVIEWER,
        "submitted_at": submitted_at,
        **trusted_metadata,
    }
    if include_metadata:
        parsed["metadata"] = metadata
    return parsed


def trusted_reviews(
    reviews: list[dict[str, Any]],
    *,
    reviewers: list[str],
    repo: str,
    pr_number: int,
    include_metadata: bool = False,
) -> list[dict[str, Any]]:
    """Filter and stably order trusted durable reviews from oldest to newest."""
    candidates: list[tuple[dt.datetime, int, dict[str, Any]]] = []
    for review in reviews:
        user = review.get("user")
        login = user.get("login") if isinstance(user, dict) and user.get("type") == "Bot" else None
        if not isinstance(login, str) or login.casefold() != TRUSTED_METADATA_AUTHOR.casefold():
            continue
        submitted_at = parse_timestamp(review.get("submitted_at"))
        review_id = review.get("id")
        if submitted_at is None or isinstance(review_id, bool) or not isinstance(review_id, int) or review_id <= 0:
            return []
        candidates.append((submitted_at, review_id, review))

    trusted: list[dict[str, Any]] = []
    for _, _, review in sorted(candidates):
        parsed = parse_trusted_review(
            review,
            reviewers=reviewers,
            repo=repo,
            pr_number=pr_number,
            include_metadata=include_metadata,
        )
        if parsed is None:
            trusted.clear()
        else:
            trusted.append(parsed)
    return trusted


def count_trusted_full_reviews(
    reviews: list[dict[str, Any]],
    *,
    reviewers: list[str],
    repo: str,
    pr_number: int,
) -> int:
    """Count trusted full AI verdicts across the pull request lifetime."""
    return len(
        trusted_full_ai_reviews(
            reviews,
            reviewers=reviewers,
            repo=repo,
            pr_number=pr_number,
        )
    )


def trusted_full_ai_reviews(
    reviews: list[dict[str, Any]],
    *,
    reviewers: list[str],
    repo: str,
    pr_number: int,
) -> list[dict[str, Any]]:
    """Return unique trusted decisive AI reviews in stable order."""
    candidates = []
    for review in reviews:
        parsed = parse_trusted_review(
            review,
            reviewers=reviewers,
            repo=repo,
            pr_number=pr_number,
            include_metadata=True,
        )
        if parsed is None or parsed["verdict"] not in {"approved", "requested_changes"}:
            continue
        provider = parsed["metadata"].get("provider")
        if not isinstance(provider, str) or not provider.strip():
            continue
        if provider.strip().casefold() == "human-override":
            continue
        submitted_at = parse_timestamp(parsed["submitted_at"])
        assert submitted_at is not None
        candidates.append((submitted_at, parsed["review_id"], parsed))

    full_reviews = []
    counted_invocations: set[tuple[str, str, str]] = set()
    for _, _, parsed in sorted(candidates):
        review_invocation_id = parsed.get("review_invocation_id")
        if isinstance(review_invocation_id, str):
            invocation_target = (
                review_invocation_id,
                parsed["head_sha"],
                parsed["diff_signature"]["value"],
            )
            if invocation_target in counted_invocations:
                continue
            counted_invocations.add(invocation_target)
        full_reviews.append({key: value for key, value in parsed.items() if key != "metadata"})
    return full_reviews


def resolve_historical_heads(
    api: Any,
    repo: str,
    head_shas: list[str],
) -> dict[str, dict[str, Any] | None]:
    """Resolve every distinct historical head in one aliased GraphQL request."""
    try:
        owner, name = repo.split("/", 1)
    except ValueError as exc:
        raise RuntimeError(f"Invalid repository name: {repo}") from exc

    unique_heads = sorted(set(head_shas))
    if not unique_heads:
        return {}
    if any(not _COMMIT_PATTERN.fullmatch(head_sha) for head_sha in unique_heads):
        raise RuntimeError("Historical review head is not a full commit SHA")

    selections = "\n".join(
        f'head{index}: object(oid: "{head_sha}") {{ ... on Commit {{ oid authoredDate messageHeadline }} }}'
        for index, head_sha in enumerate(unique_heads)
    )
    query = (
        "query ReviewHeads($owner: String!, $name: String!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{selections}\n"
        "  }\n"
        "}"
    )
    response = api.request_json(
        "POST",
        "graphql",
        payload={"query": query, "variables": {"owner": owner, "name": name}},
    )
    if not isinstance(response, dict) or response.get("errors"):
        raise RuntimeError(_HISTORICAL_HEAD_RESOLUTION_ERROR)
    data = response.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(repository, dict):
        raise RuntimeError(_HISTORICAL_HEAD_RESOLUTION_ERROR)

    resolved: dict[str, dict[str, Any] | None] = {}
    for index, head_sha in enumerate(unique_heads):
        commit = repository.get(f"head{index}")
        if commit is None:
            resolved[head_sha] = None
            continue
        if not isinstance(commit, dict) or commit.get("oid", "").lower() != head_sha.lower():
            raise RuntimeError(_HISTORICAL_HEAD_RESOLUTION_ERROR)
        resolved[head_sha] = {
            "oid": str(commit["oid"]).lower(),
            "authoredDate": commit.get("authoredDate"),
            "messageHeadline": commit.get("messageHeadline"),
        }
    return resolved


def collect_review_history(
    api: Any,
    *,
    repo: str,
    pr_number: int,
    reviewers: list[str],
) -> dict[str, Any]:
    """Collect the paginated inputs needed by the review-loop evaluator."""
    pull_request = api.request_json("GET", f"repos/{repo}/pulls/{pr_number}")
    if not isinstance(pull_request, dict):
        raise RuntimeError("Unexpected pull request response")

    reviews = api.paginate_list(f"repos/{repo}/pulls/{pr_number}/reviews")
    comments = api.paginate_list(f"repos/{repo}/pulls/{pr_number}/comments")
    issue_comments = api.paginate_list(f"repos/{repo}/issues/{pr_number}/comments")
    events = api.paginate_list(f"repos/{repo}/issues/{pr_number}/events")
    durable_reviews = trusted_reviews(
        reviews,
        reviewers=reviewers,
        repo=repo,
        pr_number=pr_number,
    )
    breaker_reviews = trusted_full_ai_reviews(
        reviews,
        reviewers=reviewers,
        repo=repo,
        pr_number=pr_number,
    )
    heads = resolve_historical_heads(
        api,
        repo,
        [item["head_sha"] for item in durable_reviews],
    )

    return {
        "pull_request": pull_request,
        "reviews": durable_reviews,
        "breaker_reviews": breaker_reviews,
        "root_comments": [comment for comment in comments if not comment.get("in_reply_to_id")],
        "issue_comments": issue_comments,
        "events": events,
        "heads": heads,
    }
