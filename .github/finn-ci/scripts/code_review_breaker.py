#!/usr/bin/env python3
"""Evaluate deterministic FINN AI review-loop circuit-breaker signals."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if __package__ in {None, ""}:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.code_review.code_review_context import (
    BREAKER_RESET_COMMAND,
    BREAKER_RESET_CTO,
    BREAKER_RESET_USERS,
    breaker_reset_authorization,
)
from scripts.code_review_history import collect_review_history, parse_timestamp
from scripts.finn_ai_coder_github_app import github_api_from_env

BREAKER_LABEL = "ai-review-breaker"
LEGACY_BREAKER_RESET_MARKER = "finn-ai-review-breaker-reset/v1"
BREAKER_RESET_MARKER = "finn-ai-review-breaker-reset/v2"
TRUSTED_RESET_ACTOR = "github-actions[bot]"
DEFAULT_REVIEWER = "finn-ai-coder[bot]"
_RESET_MARKERS = (LEGACY_BREAKER_RESET_MARKER, BREAKER_RESET_MARKER)
_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_REVIEW_SUBMITTED_AT_FIELD = "review.submitted_at"


def _required_timestamp(value: Any, field: str):
    parsed = parse_timestamp(value)
    if parsed is None:
        raise RuntimeError(f"Invalid canonical timestamp for {field}")
    return parsed


def _reviewer_logins(reviewers: list[str]) -> tuple[list[str], set[str]]:
    by_key = {reviewer.casefold(): reviewer for reviewer in reviewers}
    ordered = [by_key[key] for key in sorted(by_key)]
    return ordered, set(by_key)


def _breaker_events(events: list[dict[str, Any]], event_name: str) -> list[tuple[Any, int, dict[str, Any]]]:
    matches = []
    for event in events:
        label = event.get("label") or {}
        if event.get("event") != event_name or label.get("name", "").casefold() != BREAKER_LABEL.casefold():
            continue
        event_time = _required_timestamp(event.get("created_at"), f"breaker {event_name} event")
        matches.append((event_time, int(event.get("id") or 0), event))
    return sorted(matches)


def latest_breaker_trip(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    trips = _breaker_events(events, "labeled")
    return trips[-1][2] if trips else None


def _reset_authorization_is_valid(metadata: dict[str, Any], marker: str) -> bool:
    actor = metadata["actor"]
    if marker == LEGACY_BREAKER_RESET_MARKER:
        return actor == BREAKER_RESET_CTO

    review_count = metadata.get("review_count")
    if isinstance(review_count, bool) or not isinstance(review_count, int) or review_count < 0:
        return False
    authorization = breaker_reset_authorization(actor, review_count)
    return (
        authorization.authorized
        and metadata.get("authorization_tier") == authorization.tier
        and metadata.get("review_limit") == authorization.limit
    )


def _reset_metadata(comment: dict[str, Any], *, repo: str, pr_number: int) -> dict[str, Any] | None:
    user = comment.get("user") or {}
    body = comment.get("body")
    if (
        user.get("type") != "Bot"
        or str(user.get("login") or "").casefold() != TRUSTED_RESET_ACTOR.casefold()
        or not isinstance(body, str)
    ):
        return None
    stripped = body.rstrip()
    openings = [(marker, f"<!-- {marker}\n") for marker in _RESET_MARKERS if f"<!-- {marker}\n" in stripped]
    if len(openings) != 1 or stripped.count(openings[0][1]) != 1 or not stripped.endswith("\n-->"):
        return None
    marker, opening = openings[0]
    start = stripped.find(opening) + len(opening)
    try:
        metadata = json.loads(stripped[start:-4])
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None
    if (
        metadata.get("schema") != marker
        or metadata.get("repository") != repo
        or metadata.get("pull_request") != pr_number
        or metadata.get("actor") not in BREAKER_RESET_USERS
        or isinstance(metadata.get("trigger_comment_id"), bool)
        or not isinstance(metadata.get("trigger_comment_id"), int)
        or metadata["trigger_comment_id"] <= 0
        or isinstance(metadata.get("trip_event_id"), bool)
        or not isinstance(metadata.get("trip_event_id"), int)
        or metadata["trip_event_id"] <= 0
        or not isinstance(metadata.get("head_sha"), str)
        or not _COMMIT_PATTERN.fullmatch(metadata["head_sha"])
        or parse_timestamp(comment.get("created_at")) is None
    ):
        return None
    if not _reset_authorization_is_valid(metadata, marker):
        return None
    return metadata


def _is_target_issue_url(value: Any, *, repo: str, pr_number: int) -> bool:
    if not isinstance(value, str):
        return False
    path = urlparse(value).path.rstrip("/")
    return path.casefold() == f"/repos/{repo}/issues/{pr_number}".casefold()


def _authorized_reset_command(
    comment: dict[str, Any],
    *,
    metadata: dict[str, Any],
    repo: str,
    pr_number: int,
    trip_time: Any,
    record_time: Any,
) -> bool:
    user = comment.get("user") or {}
    actor = metadata["actor"]
    if (
        comment.get("id") != metadata["trigger_comment_id"]
        or comment.get("body") != BREAKER_RESET_COMMAND
        or comment.get("in_reply_to_id")
        or comment.get("parent_id")
        or comment.get("reply_to_id")
        or user.get("type") != "User"
        or str(user.get("login") or "").casefold() != actor.casefold()
        or actor not in BREAKER_RESET_USERS
        or not _is_target_issue_url(comment.get("issue_url"), repo=repo, pr_number=pr_number)
    ):
        return False
    command_time = parse_timestamp(comment.get("created_at"))
    return command_time is not None and trip_time < command_time <= record_time


def trusted_reset_records(
    issue_comments: list[dict[str, Any]],
    *,
    repo: str,
    pr_number: int,
    trip: dict[str, Any],
) -> list[dict[str, Any]]:
    trip_event_id = int(trip.get("id") or 0)
    trip_time = _required_timestamp(trip.get("created_at"), "breaker labeled event")
    records = []
    for comment in issue_comments:
        metadata = _reset_metadata(comment, repo=repo, pr_number=pr_number)
        if metadata is None or metadata["trip_event_id"] != trip_event_id:
            continue
        record_time = _required_timestamp(comment.get("created_at"), "breaker reset record")
        trigger = next(
            (candidate for candidate in issue_comments if candidate.get("id") == metadata["trigger_comment_id"]),
            None,
        )
        if trigger is None or not _authorized_reset_command(
            trigger,
            metadata=metadata,
            repo=repo,
            pr_number=pr_number,
            trip_time=trip_time,
            record_time=record_time,
        ):
            continue
        records.append(
            (
                record_time,
                int(comment.get("id") or 0),
                {"comment": comment, "metadata": metadata},
            )
        )
    return [record for _, _, record in sorted(records)]


def breaker_is_active(
    *,
    labels: list[Any],
    events: list[dict[str, Any]],
    issue_comments: list[dict[str, Any]],
    repo: str,
    pr_number: int,
) -> bool:
    label_present = any(
        str(label.get("name") if isinstance(label, dict) else label).casefold() == BREAKER_LABEL.casefold()
        for label in labels
    )
    trip = latest_breaker_trip(events)
    if trip is None:
        return label_present
    if trusted_reset_records(issue_comments, repo=repo, pr_number=pr_number, trip=trip):
        return False
    return True


def _epoch(  # NOSONAR - Keep fail-closed reset epoch selection together for audit.
    snapshot: dict[str, Any], *, repo: str, pr_number: int
) -> dict[str, Any]:
    pull_request = snapshot["pull_request"]
    created_at = pull_request.get("created_at")
    created_time = _required_timestamp(created_at, "pull_request.created_at")
    events = snapshot.get("events", [])
    trip = latest_breaker_trip(events)
    if trip is None:
        return {
            "source": "pull_request_created",
            "started_at": created_at,
            "event_id": None,
            "_time": created_time,
        }
    trip_id = int(trip.get("id") or 0)
    records = trusted_reset_records(
        snapshot.get("issue_comments", []),
        repo=repo,
        pr_number=pr_number,
        trip=trip,
    )
    if records:
        record = records[-1]["comment"]
        record_time = _required_timestamp(record.get("created_at"), "breaker reset record")
        return {
            "source": "approved_breaker_reset",
            "started_at": record["created_at"],
            "event_id": int(record.get("id") or 0),
            "trip_event_id": trip_id,
            "_time": record_time,
        }
    return {
        "source": "pull_request_created",
        "started_at": created_at,
        "event_id": None,
        "_time": created_time,
    }


def _rounds_in_epoch(
    reviews: list[dict[str, Any]],
    *,
    epoch_time: Any,
    reviewer_keys: set[str],
) -> list[dict[str, Any]]:
    rounds = []
    for review in reviews:
        reviewer = review.get("reviewer")
        if not isinstance(reviewer, str) or reviewer.casefold() not in reviewer_keys:
            continue
        submitted_time = _required_timestamp(review.get("submitted_at"), _REVIEW_SUBMITTED_AT_FIELD)
        if submitted_time <= epoch_time:
            continue
        rounds.append((submitted_time, int(review.get("review_id") or 0), review))
    rounds.sort(key=lambda item: (item[0], item[1]))
    return [review for _, _, review in rounds]


def _reviewer_comments(
    snapshot: dict[str, Any],
    reviewer_keys: set[str],
) -> list[tuple[Any, int, dict[str, Any]]]:
    comments = []
    for comment in snapshot.get("root_comments", []):
        if comment.get("in_reply_to_id"):
            continue
        user = comment.get("user") or {}
        login = user.get("login")
        if user.get("type") != "Bot" or not isinstance(login, str) or login.casefold() not in reviewer_keys:
            continue
        created_time = _required_timestamp(comment.get("created_at"), "review comment.created_at")
        comments.append((created_time, int(comment.get("id") or 0), comment))
    return sorted(comments, key=lambda item: (item[0], item[1]))


def _head_details(
    head: dict[str, Any] | None,
    *,
    previous_submitted_time: Any | None,
) -> tuple[str, str | None, str | None]:
    if not isinstance(head, dict) or not head.get("authoredDate"):
        return "unknown", None, None
    authored_at = head["authoredDate"]
    authored_time = _required_timestamp(authored_at, "head.authoredDate")
    headline = head.get("messageHeadline")
    if previous_submitted_time is None:
        kind = "initial"
    elif authored_time <= previous_submitted_time:
        kind = "rebase_only"
    else:
        kind = "authored_change"
    return kind, authored_at, str(headline) if headline is not None else None


def _markdown_summary(signals: dict[str, Any], trips: list[str]) -> str:
    findings = ", ".join(str(count) for count in signals["finding_counts"]) or "none"
    trip_text = ", ".join(trips) or "none"
    return (
        "### AI review circuit breaker\n\n"
        f"- Epoch rounds: {signals['round_count']}\n"
        f"- Findings by round: {findings}\n"
        f"- Consecutive rebase-only rounds: {signals['consecutive_rebase_only']}\n"
        f"- Trips: {trip_text}"
    )


def evaluate_review_loop(
    snapshot: dict[str, Any],
    *,
    repo: str,
    pr_number: int,
    reviewers: list[str],
) -> dict[str, Any]:
    """Purely evaluate review-loop trajectory from a collected history snapshot."""
    ordered_reviewers, reviewer_keys = _reviewer_logins(reviewers)
    epoch = _epoch(snapshot, repo=repo, pr_number=pr_number)
    history_rounds = _rounds_in_epoch(
        snapshot["reviews"],
        epoch_time=epoch["_time"],
        reviewer_keys=reviewer_keys,
    )
    breaker_review_ids = {
        review["review_id"]
        for review in _rounds_in_epoch(
            snapshot["breaker_reviews"],
            epoch_time=epoch["_time"],
            reviewer_keys=reviewer_keys,
        )
    }
    comments = _reviewer_comments(snapshot, reviewer_keys)
    heads = snapshot.get("heads", {})

    evaluated_rounds = []
    previous_submitted_time = None
    window_start = epoch["_time"]
    for review in history_rounds:
        submitted_time = _required_timestamp(review["submitted_at"], _REVIEW_SUBMITTED_AT_FIELD)
        finding_count = sum(1 for comment_time, _, _ in comments if window_start < comment_time <= submitted_time)
        window_start = submitted_time
        if review["review_id"] not in breaker_review_ids:
            continue
        if finding_count == 0 and review.get("verdict") == "requested_changes":
            finding_count = 1
        head_kind, authored_at, headline = _head_details(
            heads.get(review["head_sha"]),
            previous_submitted_time=previous_submitted_time,
        )
        evaluated_rounds.append(
            {
                "number": len(evaluated_rounds) + 1,
                "review_id": review["review_id"],
                "review_url": review.get("review_url", ""),
                "reviewer": review["reviewer"],
                "submitted_at": review["submitted_at"],
                "verdict": review["verdict"],
                "head_sha": review["head_sha"],
                "head_authored_at": authored_at,
                "head_message": headline,
                "head_kind": head_kind,
                "findings": finding_count,
            }
        )
        previous_submitted_time = submitted_time

    finding_counts = [item["findings"] for item in evaluated_rounds]
    transitions = [current - previous for previous, current in zip(finding_counts, finding_counts[1:])]
    consecutive_rebase_only = 0
    for item in reversed(evaluated_rounds):
        if item["head_kind"] != "rebase_only":
            break
        consecutive_rebase_only += 1

    trips = []
    if len(evaluated_rounds) >= 5:
        trips.append("max_rounds")
    if len(evaluated_rounds) >= 4 and all(change >= 0 for change in transitions[-3:]) and finding_counts[-1] > 0:
        trips.append("flat_or_rising")
    if consecutive_rebase_only >= 2:
        trips.append("rebase_only")

    signals = {
        "round_count": len(evaluated_rounds),
        "finding_counts": finding_counts,
        "finding_transitions": transitions,
        "consecutive_rebase_only": consecutive_rebase_only,
    }
    public_epoch = {key: value for key, value in epoch.items() if key != "_time"}
    return {
        "pr": {
            "repository": repo,
            "number": pr_number,
            "created_at": snapshot["pull_request"]["created_at"],
        },
        "reviewers": ordered_reviewers,
        "epoch": public_epoch,
        "rounds": evaluated_rounds,
        "signals": signals,
        "trips": trips,
        "status": "stop" if trips else "continue",
        "summary": _markdown_summary(signals, trips),
    }


def render_evaluation(evaluation: dict[str, Any]) -> str:
    return json.dumps(evaluation, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--reviewer", action="append", default=[])
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    reviewers = args.reviewer or [DEFAULT_REVIEWER]
    api = github_api_from_env(prefer_github_token=True)
    snapshot = collect_review_history(
        api,
        repo=args.repo,
        pr_number=args.pr_number,
        reviewers=reviewers,
    )
    evaluation = evaluate_review_loop(
        snapshot,
        repo=args.repo,
        pr_number=args.pr_number,
        reviewers=reviewers,
    )
    rendered = render_evaluation(evaluation)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")  # NOSONAR - The workflow controls this output path.
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
