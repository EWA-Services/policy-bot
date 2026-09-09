#!/usr/bin/env python3
"""Decide whether a full FINN AI review should run for the current patch."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.code_review_breaker import breaker_is_active
from scripts.code_review_history import DIFF_SIGNATURE_ALGORITHM, trusted_reviews
from scripts.finn_ai_coder_github_app import (
    CHECK_NAME,
    default_run_url,
    diff_signature,
    fetch_pr_diff,
    github_api_from_env,
    output_values,
    upsert_check_run,
)

BREAKER_LABEL = "ai-review-breaker"
DEFAULT_REVIEWER = "finn-ai-coder[bot]"


def emit_warning(message: str) -> None:
    print(f"::warning::{message}")


def read_live_diff_signature(api: Any, repo: str, pr_number: int) -> str:
    return diff_signature(fetch_pr_diff(api, repo, str(pr_number)))


def require_live_diff_signature(
    api: Any,
    *,
    repo: str,
    pr_number: int,
    expected_signature: str,
    signature_reader: Callable[[Any, str, int], str] | None = None,
) -> None:
    reader = signature_reader or read_live_diff_signature
    if reader(api, repo, pr_number) != expected_signature:
        raise RuntimeError("Pull request normalized diff changed after diff signature resolution")


def _label_names(labels: list[Any]) -> set[str]:
    names: set[str] = set()
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.add(name.casefold())
    return names


def _unchanged_summary(review: dict[str, Any], signature: str) -> str:
    return (
        f"Reused trusted review {review['review_id']} from prior head `{review['head_sha']}` "
        "because the normalized patch is unchanged.\n\n"
        f"Signature: `{DIFF_SIGNATURE_ALGORITHM}:{signature}`"
    )


def decide_pre_review(
    *,
    labels: list[Any],
    current_signature: str,
    reviews: list[dict[str, Any]],
    current_head_sha: str = "",
    force_full_review: bool = False,
    breaker_active: bool | None = None,
) -> dict[str, Any]:
    """Return one gate decision from trusted reviews ordered oldest first."""
    if breaker_active is None:
        breaker_active = BREAKER_LABEL.casefold() in _label_names(labels)
    if breaker_active:
        return {
            "action": "skip_breaker",
            "conclusion": "action_required",
            "title": "AI review paused",
            "summary": "AI review is paused by the ai-review-breaker label.",
        }

    if force_full_review:
        return {"action": "run"}

    matching = [
        review
        for review in reviews
        if review.get("diff_signature", {}).get("algorithm") == DIFF_SIGNATURE_ALGORITHM
        and review.get("diff_signature", {}).get("value") == current_signature
        and (
            review.get("metadata", {}).get("provider") != "human-override" or review.get("head_sha") == current_head_sha
        )
    ]
    if not matching:
        return {"action": "run"}

    newest = matching[-1]
    verdict = newest.get("verdict")
    if verdict not in {"approved", "requested_changes"}:
        return {"action": "run"}
    approved = verdict == "approved"
    return {
        "action": "skip_unchanged",
        "conclusion": "success" if approved else "failure",
        "title": "AI review approved" if approved else "AI review requested changes",
        "summary": _unchanged_summary(newest, current_signature),
        "review": newest,
    }


def notice_for_decision(decision: dict[str, Any]) -> str:
    if decision.get("action") != "skip_unchanged":
        return ""
    return f"::notice::patch unchanged since review {decision['review']['review_id']}; skipping"


def evaluate_github_gate(
    read_api: Any,
    *,
    repo: str,
    pr_number: int,
    expected_head: str,
    current_signature: str,
    reviewers: list[str],
    force_full_review: bool = False,
) -> tuple[dict[str, Any], str]:
    pull_request = read_api.request_json("GET", f"repos/{repo}/pulls/{pr_number}")
    if not isinstance(pull_request, dict):
        raise RuntimeError("Unexpected pull request response")
    head_sha = str((pull_request.get("head") or {}).get("sha") or "")
    if not head_sha:
        raise RuntimeError("Pull request has no head SHA")
    if head_sha != expected_head:
        raise RuntimeError("Pull request head changed after diff signature resolution")

    reviews = read_api.paginate_list(f"repos/{repo}/pulls/{pr_number}/reviews")
    durable_reviews = trusted_reviews(
        reviews,
        reviewers=reviewers,
        repo=repo,
        pr_number=pr_number,
        include_metadata=True,
    )
    labels = list(pull_request.get("labels") or [])
    active = breaker_is_active(
        labels=labels,
        events=read_api.paginate_list(f"repos/{repo}/issues/{pr_number}/events"),
        issue_comments=read_api.paginate_list(f"repos/{repo}/issues/{pr_number}/comments"),
        repo=repo,
        pr_number=pr_number,
    )
    decision = decide_pre_review(
        labels=labels,
        current_signature=current_signature,
        current_head_sha=head_sha,
        reviews=durable_reviews,
        force_full_review=force_full_review,
        breaker_active=active,
    )
    if active and BREAKER_LABEL.casefold() not in _label_names(labels):
        decision["restore_label"] = True
    return decision, head_sha


def review_check_values(decision: dict[str, Any], *, model: str) -> dict[str, Any]:
    if decision["action"] != "run":
        return {
            "status": "completed",
            "conclusion": decision["conclusion"],
            "title": decision["title"],
            "summary": decision["summary"],
        }

    summary = "finn-ai-coder accepted an authorized review trigger and is reviewing the current PR head."
    if model:
        summary = f"{summary}\n\nModel: {model}"
    return {
        "status": "in_progress",
        "conclusion": None,
        "title": "AI review in progress",
        "summary": summary,
    }


def apply_github_gate(
    read_api: Any,  # NOSONAR - Explicit gate inputs and injectable side-effect seams stay separate.
    write_api: Any,
    *,
    repo: str,
    pr_number: int,
    expected_head: str,
    current_signature: str,
    reviewers: list[str],
    force_full_review: bool = False,
    model: str = "",
    details_url: str = "",
    external_id: str = "",
    check_writer: Callable[..., dict[str, Any]] | None = None,
    signature_reader: Callable[[Any, str, int], str] | None = None,
    warning_writer: Callable[[str], None] = emit_warning,
    label_api: Any | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Make one gate decision and attempt one corresponding check projection."""
    decision, head_sha = evaluate_github_gate(
        read_api,
        repo=repo,
        pr_number=pr_number,
        expected_head=expected_head,
        current_signature=current_signature,
        reviewers=reviewers,
        force_full_review=force_full_review,
    )
    if decision.get("restore_label"):
        try:
            (label_api or write_api).request_json(
                "POST",
                f"repos/{repo}/issues/{pr_number}/labels",
                payload={"labels": [BREAKER_LABEL]},
            )
        except Exception:
            warning_writer("Could not restore the AI review breaker label.")
    if decision["action"] != "skip_breaker":
        require_live_diff_signature(
            read_api,
            repo=repo,
            pr_number=pr_number,
            expected_signature=current_signature,
            signature_reader=signature_reader,
        )

    values = review_check_values(decision, model=model)
    writer = check_writer or upsert_check_run
    try:
        check = writer(
            write_api,
            repo=repo,
            sha=head_sha,
            name=CHECK_NAME,
            details_url=details_url,
            external_id=external_id,
            **values,
        )
    except Exception:
        warning_writer("Could not publish the finn-ai-coder review check.")
        check = {}
    return decision, head_sha, check


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--diff-signature", required=True)
    parser.add_argument("--reviewer", action="append", default=[])
    parser.add_argument("--force-full-review", choices=("true", "false"), default="false")
    parser.add_argument("--model", default="")
    parser.add_argument("--details-url", default="")
    parser.add_argument("--external-id", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    reviewers = args.reviewer or [DEFAULT_REVIEWER]
    workflow_api = github_api_from_env(prefer_github_token=True)
    decision, head_sha, check = apply_github_gate(
        workflow_api,
        github_api_from_env(),
        repo=args.repo,
        pr_number=args.pr_number,
        expected_head=args.expected_head,
        current_signature=args.diff_signature,
        reviewers=reviewers,
        force_full_review=args.force_full_review == "true",
        model=args.model,
        details_url=args.details_url or default_run_url(args.repo),
        external_id=args.external_id,
        label_api=workflow_api,
    )
    output_values(
        {
            "action": decision["action"],
            "head_sha": head_sha,
            "diff_signature": args.diff_signature,
            "check_run_id": check.get("id", ""),
        }
    )
    notice = notice_for_decision(decision)
    print(notice or f"Pre-review gate action: {decision['action']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
