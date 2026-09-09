#!/usr/bin/env python3
"""Pause a stopped AI review loop and publish its deterministic explanation."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

if __package__ in {None, ""}:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.code_review.code_review_context import (
    BREAKER_RESET_COMMAND,
    BREAKER_RESET_USERS,
    breaker_reset_authorization,
)
from scripts.code_review.code_review_results import structured_output_text
from scripts.code_review_breaker import (
    BREAKER_RESET_MARKER,
    DEFAULT_REVIEWER,
    breaker_is_active,
    latest_breaker_trip,
    trusted_reset_records,
)
from scripts.code_review_history import count_trusted_full_reviews, parse_timestamp
from scripts.finn_ai_coder_github_app import (
    CHECK_NAME,
    default_run_url,
    github_api_from_env,
    output_values,
    upsert_check_run,
)

BREAKER_LABEL = "ai-review-breaker"
BREAKER_LABEL_DESCRIPTION = "AI review halted; an approved user runs /ai-review-breaker-reset to resume"
RESUME_SENTENCE = "If you are approved to resume AI review, comment `/ai-review-breaker-reset`."


def validate_inputs(*, pr_number: str) -> None:
    if not re.fullmatch(r"[1-9][0-9]*", pr_number):  # NOSONAR - PR numbers are ASCII.
        raise ValueError("PR number must be a canonical positive decimal integer")


def _load_evaluation(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))  # NOSONAR - The checked-in action supplies this path.
    if not isinstance(value, dict):
        raise RuntimeError("Circuit-breaker evaluation must be a JSON object")
    return value


def _validate_stopped_evaluation(
    evaluation: dict[str, Any],
    *,
    repo: str,
    pr_number: int,
) -> None:
    pr = evaluation.get("pr") or {}
    if evaluation.get("status") != "stop" or pr.get("repository") != repo or pr.get("number") != pr_number:
        raise RuntimeError("Publisher requires a stopped evaluation for the target pull request")


def emit_warning(message: str) -> None:
    print(f"::warning::{message}")


def ensure_repository_label(api: Any, repo: str) -> None:
    labels = api.paginate_list(f"repos/{repo}/labels")
    existing = next(
        (label for label in labels if str(label.get("name") or "").casefold() == BREAKER_LABEL.casefold()),
        None,
    )
    if existing is None:
        api.request_json(
            "POST",
            f"repos/{repo}/labels",
            payload={
                "name": BREAKER_LABEL,
                "color": "B60205",
                "description": BREAKER_LABEL_DESCRIPTION,
            },
        )
        return
    if existing.get("description") != BREAKER_LABEL_DESCRIPTION:
        api.request_json(
            "PATCH",
            f"repos/{repo}/labels/{quote(str(existing['name']), safe='')}",
            payload={"description": BREAKER_LABEL_DESCRIPTION},
        )


def _pull_request_has_label(pull_request: dict[str, Any]) -> bool:
    return any(
        str(label.get("name") if isinstance(label, dict) else label).casefold() == BREAKER_LABEL.casefold()
        for label in pull_request.get("labels") or []
    )


def _is_target_issue_url(value: Any, *, repo: str, pr_number: int) -> bool:
    if not isinstance(value, str):
        return False
    return urlparse(value).path.rstrip("/") == f"/repos/{repo}/issues/{pr_number}"


def _reset_record_body(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    actor: str,
    comment_id: int,
    trip_event_id: int,
    authorization_tier: str,
    review_count: int,
    review_limit: int | None,
) -> str:
    metadata = {
        "actor": actor,
        "authorization_tier": authorization_tier,
        "head_sha": head_sha,
        "pull_request": pr_number,
        "repository": repo,
        "review_count": review_count,
        "review_limit": review_limit,
        "schema": BREAKER_RESET_MARKER,
        "trigger_comment_id": comment_id,
        "trip_event_id": trip_event_id,
    }
    encoded = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
    return (
        f"AI review reset was authorized by @{actor} for the current commit.\n\n"
        f"<!-- {BREAKER_RESET_MARKER}\n{encoded}\n-->"
    )


def prepare_reset(  # NOSONAR - Keep authorization, durable write, and label projection visibly ordered.
    api: Any,
    *,
    repo: str,
    pr_number: int,
    expected_head: str,
    actor: str,
    comment_id: int,
) -> dict[str, Any]:
    """Validate one approved reset command, record it, and remove the label."""
    pull_request = api.request_json("GET", f"repos/{repo}/pulls/{pr_number}")
    if not isinstance(pull_request, dict):
        raise RuntimeError("Unexpected pull request response")
    current_head = str((pull_request.get("head") or {}).get("sha") or "")
    if pull_request.get("state") != "open" or not current_head or current_head != expected_head:
        raise RuntimeError("Breaker reset target is not the expected open pull request head")

    trigger = api.request_json("GET", f"repos/{repo}/issues/comments/{comment_id}")
    user = trigger.get("user") if isinstance(trigger, dict) else None
    if (
        actor not in BREAKER_RESET_USERS
        or not isinstance(trigger, dict)
        or trigger.get("id") != comment_id
        or trigger.get("body") != BREAKER_RESET_COMMAND
        or parse_timestamp(trigger.get("created_at")) is None
        or not isinstance(user, dict)
        or user.get("type") != "User"
        or user.get("login") != actor
        or not _is_target_issue_url(trigger.get("issue_url"), repo=repo, pr_number=pr_number)
    ):
        raise RuntimeError("GitHub did not confirm an authorized breaker reset command")

    events = api.paginate_list(f"repos/{repo}/issues/{pr_number}/events")
    issue_comments = api.paginate_list(f"repos/{repo}/issues/{pr_number}/comments")
    trip = latest_breaker_trip(events)
    if trip is None:
        if _pull_request_has_label(pull_request):
            raise RuntimeError("GitHub did not expose the active breaker trip")
        return {"action": "not_active", "head_sha": current_head, "reset_record_id": ""}
    trigger_time = parse_timestamp(trigger.get("created_at"))
    trip_time = parse_timestamp(trip.get("created_at"))
    if trigger_time is None or trip_time is None or trigger_time <= trip_time:
        return {"action": "not_active", "head_sha": current_head, "reset_record_id": ""}
    trip_event_id = int(trip.get("id") or 0)
    records = trusted_reset_records(
        issue_comments,
        repo=repo,
        pr_number=pr_number,
        trip=trip,
    )
    existing = next(
        (record for record in records if record["metadata"].get("trigger_comment_id") == comment_id),
        None,
    )
    label_present = _pull_request_has_label(pull_request)
    if existing is not None:
        if label_present:
            api.request_json(
                "DELETE",
                f"repos/{repo}/issues/{pr_number}/labels/{quote(BREAKER_LABEL, safe='')}",
            )
            return {
                "action": "duplicate",
                "head_sha": current_head,
                "reset_record_id": str(existing["comment"].get("id") or ""),
            }
        return {
            "action": "duplicate",
            "head_sha": current_head,
            "reset_record_id": str(existing["comment"].get("id") or ""),
        }
    if not breaker_is_active(
        labels=list(pull_request.get("labels") or []),
        events=events,
        issue_comments=issue_comments,
        repo=repo,
        pr_number=pr_number,
    ):
        return {"action": "not_active", "head_sha": current_head, "reset_record_id": ""}

    reviews = api.paginate_list(f"repos/{repo}/pulls/{pr_number}/reviews")
    review_count = count_trusted_full_reviews(
        reviews,
        reviewers=[DEFAULT_REVIEWER],
        repo=repo,
        pr_number=pr_number,
    )
    authorization = breaker_reset_authorization(actor, review_count)
    if not authorization.authorized:
        return {
            "action": "budget_exhausted",
            "authorization_tier": authorization.tier,
            "head_sha": current_head,
            "reset_record_id": "",
            "review_count": review_count,
            "review_limit": str(authorization.limit or ""),
        }

    record = api.request_json(
        "POST",
        f"repos/{repo}/issues/{pr_number}/comments",
        payload={
            "body": _reset_record_body(
                repo=repo,
                pr_number=pr_number,
                head_sha=current_head,
                actor=actor,
                comment_id=comment_id,
                trip_event_id=trip_event_id,
                authorization_tier=authorization.tier,
                review_count=review_count,
                review_limit=authorization.limit,
            )
        },
    )
    record_id = record.get("id") if isinstance(record, dict) else None
    if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0:
        raise RuntimeError("GitHub did not create the durable breaker reset record")

    if label_present:
        api.request_json(
            "DELETE",
            f"repos/{repo}/issues/{pr_number}/labels/{quote(BREAKER_LABEL, safe='')}",
        )
    return {"action": "applied", "head_sha": current_head, "reset_record_id": str(record_id)}


def prepare_pause(  # NOSONAR - Keep stale-state validation and pause projections visibly ordered.
    api: Any,
    *,
    repo: str,
    pr_number: int,
    expected_head: str,
    evaluation: dict[str, Any],
    details_url: str = "",
    external_id: str = "",
    check_writer: Callable[..., dict[str, Any]] | None = None,
    warning_writer: Callable[[str], None] = emit_warning,
) -> dict[str, Any]:
    """Reject a stale target, then attempt the visible pause projections once."""
    _validate_stopped_evaluation(evaluation, repo=repo, pr_number=pr_number)
    pull_request = api.request_json("GET", f"repos/{repo}/pulls/{pr_number}")
    if not isinstance(pull_request, dict):
        raise RuntimeError("Unexpected pull request response")
    current_head = str((pull_request.get("head") or {}).get("sha") or "")
    if not current_head or current_head != expected_head:
        raise RuntimeError("Pull request head changed before the circuit breaker could pause it")

    epoch = evaluation.get("epoch") or {}
    if epoch.get("source") == "approved_breaker_reset":
        events = api.paginate_list(f"repos/{repo}/issues/{pr_number}/events")
        trip_event_id = epoch.get("trip_event_id")
        trip = latest_breaker_trip(events)
        if trip is None or int(trip.get("id") or 0) != trip_event_id:
            raise RuntimeError("Stopped evaluation is stale after a newer or different breaker trip")
        records = trusted_reset_records(
            api.paginate_list(f"repos/{repo}/issues/{pr_number}/comments"),
            repo=repo,
            pr_number=pr_number,
            trip=trip,
        )
        latest_record_id = int(records[-1]["comment"].get("id") or 0) if records else None
        if latest_record_id != epoch.get("event_id"):
            raise RuntimeError("Stopped evaluation is stale after a newer or different breaker reset")
    label_applied = _pull_request_has_label(pull_request)
    try:
        ensure_repository_label(api, repo)
        if not label_applied:
            api.request_json(
                "POST",
                f"repos/{repo}/issues/{pr_number}/labels",
                payload={"labels": [BREAKER_LABEL]},
            )
        label_applied = True
    except Exception:
        warning_writer("Could not apply the AI review breaker label.")

    check_published = False
    writer = check_writer or upsert_check_run
    try:
        writer(
            api,
            repo=repo,
            sha=current_head,
            name=CHECK_NAME,
            status="completed",
            conclusion="action_required",
            title="AI review paused",
            summary=evaluation["summary"],
            details_url=details_url,
            external_id=external_id,
        )
        check_published = True
    except Exception:
        warning_writer("Could not publish the action_required AI review check.")

    return {
        "head_sha": current_head,
        "label_applied": label_applied,
        "check_published": check_published,
    }


def deterministic_explanation(evaluation: dict[str, Any]) -> str:
    trips = set(evaluation.get("trips") or [])
    reasons = []
    if "rebase_only" in trips:
        reasons.append("it reviewed the same code again after branch-only updates")
    if "flat_or_rising" in trips:
        reasons.append("recent reviews did not reduce the remaining findings")
    if "max_rounds" in trips:
        reasons.append("the pull request reached the automatic review limit")
    if not reasons:
        reasons.append("the automatic review loop did not converge")
    return (
        "Automatic AI review paused because "
        + "; and ".join(reasons)
        + ". Check the recent review history before resuming."
    )


def parse_breaker_analysis(value: object) -> dict[str, str]:
    expected_fields = {"explanation", "recommendedNextAction"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise RuntimeError("AI review-loop analysis fields do not match the published result contract")
    return {field: structured_output_text(value, field) for field in sorted(expected_fields)}


def post_explanation_comment(
    api: Any,
    *,
    repo: str,
    pr_number: int,
    expected_head: str,
    evaluation: dict[str, Any],
    analysis: object | None = None,
    warning_writer: Callable[[str], None] = emit_warning,
) -> dict[str, Any] | None:
    _validate_stopped_evaluation(evaluation, repo=repo, pr_number=pr_number)
    try:
        pull_request = api.request_json("GET", f"repos/{repo}/pulls/{pr_number}")
    except Exception:
        pull_request = None
        warning_writer(
            "Could not verify the pull request head after AI review-loop analysis; using the deterministic fallback."
        )
    current_head = str((pull_request.get("head") or {}).get("sha") or "") if isinstance(pull_request, dict) else ""
    parsed_analysis = None
    if pull_request is not None:
        if current_head != expected_head:
            warning_writer(
                "Pull request head changed during AI review-loop analysis; using the deterministic fallback."
            )
        elif analysis is not None:
            try:
                parsed_analysis = parse_breaker_analysis(analysis)
            except RuntimeError:
                warning_writer("Ignoring invalid AI review-loop analysis output.")

    if parsed_analysis is None:
        content = deterministic_explanation(evaluation)
    else:
        content = (
            f"{parsed_analysis['explanation']}\n\n"
            f"**Recommended next action:** {parsed_analysis['recommendedNextAction']}"
        )
    body = f"## AI review paused\n\n{content}\n\n{RESUME_SENTENCE}"
    try:
        posted = api.request_json(
            "POST",
            f"repos/{repo}/issues/{pr_number}/comments",
            payload={"body": body},
        )
    except Exception:
        warning_writer("Could not post the AI review explanation comment.")
        return None
    if not isinstance(posted, dict):
        warning_writer("Could not post the AI review explanation comment: unexpected GitHub response")
        return None
    return posted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    pause = subparsers.add_parser("pause")
    pause.add_argument("--repo", required=True)
    pause.add_argument("--pr-number", type=int, required=True)
    pause.add_argument("--expected-head", required=True)
    pause.add_argument("--evaluation", type=Path, required=True)
    pause.add_argument("--details-url", default="")
    pause.add_argument("--external-id", default="")

    post_comment = subparsers.add_parser("post-comment")
    post_comment.add_argument("--repo", required=True)
    post_comment.add_argument("--pr-number", type=int, required=True)
    post_comment.add_argument("--expected-head", required=True)
    post_comment.add_argument("--evaluation", type=Path, required=True)
    post_comment.add_argument("--analysis", type=Path)

    reset = subparsers.add_parser("reset")
    reset.add_argument("--repo", required=True)
    reset.add_argument("--pr-number", type=int, required=True)
    reset.add_argument("--expected-head", required=True)
    reset.add_argument("--actor", required=True)
    reset.add_argument("--comment-id", type=int, required=True)

    validate = subparsers.add_parser("validate-inputs")
    validate.add_argument("--pr-number", required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate-inputs":
        try:
            validate_inputs(pr_number=args.pr_number)
        except ValueError as error:
            parser.error(str(error))
        return
    if args.command == "reset":
        result = prepare_reset(
            github_api_from_env(prefer_github_token=True),
            repo=args.repo,
            pr_number=args.pr_number,
            expected_head=args.expected_head,
            actor=args.actor,
            comment_id=args.comment_id,
        )
        output_values(result)
        return
    evaluation = _load_evaluation(args.evaluation)
    if args.command == "pause":
        api = github_api_from_env()
        result = prepare_pause(
            api,
            repo=args.repo,
            pr_number=args.pr_number,
            expected_head=args.expected_head,
            evaluation=evaluation,
            details_url=args.details_url or default_run_url(args.repo),
            external_id=args.external_id,
        )
        output_values(result)
    else:
        api = github_api_from_env()
        analysis = None
        if args.analysis is not None and args.analysis.is_file():
            try:
                analysis = json.loads(args.analysis.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                emit_warning("Ignoring unreadable AI review-loop analysis output.")
        post_explanation_comment(
            api,
            repo=args.repo,
            pr_number=args.pr_number,
            expected_head=args.expected_head,
            evaluation=evaluation,
            analysis=analysis,
        )


if __name__ == "__main__":
    main()
