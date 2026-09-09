#!/usr/bin/env python3
"""Trigger and context resolution for the direct CLI code-review workflow."""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Any, NamedTuple

from scripts.code_review_common import GhOutput, gh_output
from scripts.override_commands import AI_REVIEW_OVERRIDE_COMMAND, parse_registered_override_command

CODEX_GPT56_MODEL = "gpt-5.6-sol"
BREAKER_RESET_COMMAND = "/ai-review-breaker-reset"
BREAKER_RESET_LEADS = frozenset({"maetolay", "bhargavms"})
BREAKER_RESET_VP_ENGINEERING = "poom"
BREAKER_RESET_CTO = "jai"
BREAKER_RESET_USERS = BREAKER_RESET_LEADS | frozenset({BREAKER_RESET_VP_ENGINEERING, BREAKER_RESET_CTO})
DEFAULT_REVIEW_OVERRIDE_APPROVERS = frozenset({"jai"})


class BreakerResetAuthorization(NamedTuple):
    authorized: bool
    tier: str
    limit: int | None


def breaker_reset_authorization(actor: str, review_count: int) -> BreakerResetAuthorization:
    if actor in BREAKER_RESET_LEADS:
        return BreakerResetAuthorization(review_count <= 5, "lead", 5)
    if actor == BREAKER_RESET_VP_ENGINEERING:
        return BreakerResetAuthorization(review_count <= 10, "vp-engineering", 10)
    if actor == BREAKER_RESET_CTO:
        return BreakerResetAuthorization(True, "cto", None)
    return BreakerResetAuthorization(False, "", None)


def parse_semver(value: str) -> tuple[tuple[int, int, int], str | None] | None:
    match = re.fullmatch(
        r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?",
        value.strip(),
    )
    if not match:
        return None
    major, minor, patch, prerelease = match.groups()
    return (int(major), int(minor), int(patch)), prerelease


def is_before_codex_gpt55_floor(codex_version: str) -> bool:
    parsed = parse_semver(codex_version)
    if not parsed:
        return False
    version, prerelease = parsed
    return version < (0, 131, 0) or (version == (0, 131, 0) and bool(prerelease))


def is_before_codex_gpt56_floor(codex_version: str) -> bool:
    parsed = parse_semver(codex_version)
    if not parsed:
        return False
    version, prerelease = parsed
    return version < (0, 144, 0) or (version == (0, 144, 0) and bool(prerelease))


def validate_codex_version(codex_version: str) -> None:
    if codex_version == "latest" or parse_semver(codex_version):
        return
    raise ValueError(f"codex_version must be 'latest' or exact semver MAJOR.MINOR.PATCH; got: {codex_version}")


def resolve_codex_model(codex_version: str, codex_model: str) -> str:
    selected_model = codex_model or CODEX_GPT56_MODEL
    if selected_model == "gpt-5.6":
        selected_model = CODEX_GPT56_MODEL
    if selected_model == CODEX_GPT56_MODEL and is_before_codex_gpt56_floor(codex_version):
        fallback_model = "gpt-5.4" if is_before_codex_gpt55_floor(codex_version) else "gpt-5.5"
        print(
            f"Codex {codex_version} does not support {CODEX_GPT56_MODEL}; falling back to {fallback_model}.",
            file=sys.stderr,
        )
        return fallback_model
    if selected_model == "gpt-5.5" and is_before_codex_gpt55_floor(codex_version):
        print(
            f"Codex {codex_version} does not support gpt-5.5; falling back to gpt-5.4.",
            file=sys.stderr,
        )
        return "gpt-5.4"
    return selected_model


def is_review_command(body: str) -> bool:
    return body == "/review"


def is_review_override_command(body: str) -> bool:
    """Return whether the body is the exact AI-review override command."""
    return (
        parse_registered_override_command(
            body,
            canonical=AI_REVIEW_OVERRIDE_COMMAND,
        )
        is not None
    )


def resolve_breaker_reset(
    *,
    body: str,
    comment_author: str,
    outputs: dict[str, str],
) -> bool:
    if body != BREAKER_RESET_COMMAND:
        return False
    if comment_author not in BREAKER_RESET_USERS:
        print("AI review breaker reset is restricted to an approved user; skipping.")
        return True
    outputs["should_run"] = "true"
    outputs["check_required"] = "true"
    outputs["run_codex"] = "true"
    outputs["force_full_review"] = "true"
    outputs["breaker_reset_actor"] = comment_author
    outputs["breaker_reset_comment_id"] = outputs["comment_id"]
    return True


def resolve_review_override(
    *,
    body: str,
    previous_body: str,
    event_action: str,
    comment_author: str,
    comment_author_has_permission: bool,
    approved_users: frozenset[str],
    outputs: dict[str, str],
) -> bool:
    """Apply or revoke an AI-review override from an approved user."""
    current_override = is_review_override_command(body)
    previous_override = is_review_override_command(previous_body)
    if event_action == "deleted" or (
        event_action == "edited"
        and previous_override
        and (not current_override or not comment_author_has_permission or comment_author not in approved_users)
    ):
        outputs["should_run"] = "true"
        outputs["check_required"] = "true"
        outputs["run_codex"] = "true"
        outputs["force_full_review"] = "true"
        return True
    if not current_override:
        return False
    if not comment_author_has_permission or comment_author not in approved_users:
        print("AI review override is restricted to an approved user; skipping.")
        return True

    outputs["should_run"] = "true"
    outputs["check_required"] = "true"
    outputs["force_full_review"] = "true"
    outputs["review_override_actor"] = comment_author
    return True


def review_override_approvers(env: dict[str, str]) -> frozenset[str]:
    """Return configured AI-review override approvers."""
    configured = env.get("AI_REVIEW_OVERRIDE_APPROVERS", "").split()
    return frozenset(configured) or DEFAULT_REVIEW_OVERRIDE_APPROVERS


def resolve_permission(
    *,
    event_name: str,
    repository: str,
    actor: str,
    run_gh: GhOutput = gh_output,
) -> tuple[bool, str]:
    if event_name == "workflow_dispatch":
        return True, "manual-dispatch"
    try:
        permission = run_gh(
            [
                "api",
                f"repos/{repository}/collaborators/{actor}/permission",
                "--jq",
                ".permission",
            ]
        ).strip()
    except subprocess.CalledProcessError:
        permission = "none"
    return permission in {"admin", "maintain", "write"}, permission


def default_context_outputs(env: dict[str, str]) -> dict[str, str]:
    codex_version = (env.get("CODEX_VERSION_INPUT") or "latest").strip()
    validate_codex_version(codex_version)
    codex_model = resolve_codex_model(codex_version, env.get("CODEX_MODEL_INPUT") or "")
    return {
        "should_run": "false",
        "check_required": "false",
        "run_codex": "false",
        "target_pr_number": "",
        "comment_author": "",
        "comment_id": "",
        "force_full_review": "false",
        "breaker_reset_actor": "",
        "breaker_reset_comment_id": "",
        "review_override_actor": "",
        "codex_version": codex_version,
        "codex_model": codex_model,
        "codex_reasoning_effort": env.get("CODEX_REASONING_EFFORT_INPUT") or "xhigh",
    }


def resolve_dispatch_context(env: dict[str, str], outputs: dict[str, str]) -> dict[str, str]:
    target_pr_number = env.get("DISPATCH_PR_NUMBER", "").strip()
    if not target_pr_number:
        raise ValueError("dispatch requires pr_number")

    outputs["should_run"] = "true"
    outputs["target_pr_number"] = target_pr_number
    outputs["comment_author"] = env.get("DISPATCH_COMMENT_AUTHOR") or "manual-dispatch"
    outputs["comment_id"] = env.get("DISPATCH_COMMENT_ID", "").strip()
    outputs["check_required"] = "true"
    outputs["force_full_review"] = "true"

    outputs["run_codex"] = "true"
    return outputs


def resolve_repository_dispatch_context(
    env: dict[str, str],
    outputs: dict[str, str],
    *,
    comment_author_has_permission: bool,
) -> dict[str, str]:
    """Validate one command dispatch and select code-review behavior."""
    body = env.get("DISPATCH_COMMENT_BODY", "")
    command = env.get("DISPATCH_COMMAND", "").strip()
    issue_state = env.get("DISPATCH_ISSUE_STATE", "").strip()
    event_action = env.get("DISPATCH_EVENT_ACTION", "").strip()
    if issue_state != "open" or event_action not in {"created", "edited", "deleted"}:
        print("Repository dispatch is missing valid open-PR lifecycle metadata; skipping.")
        return outputs

    if command == "review":
        if not is_review_command(body):
            print("Review dispatch does not contain an exact /review command; skipping.")
            return outputs
        outputs = resolve_dispatch_context(env, outputs)
        outputs["check_required"] = "true"
        outputs["force_full_review"] = "true"
        return outputs

    if command not in {"ai-review-breaker-reset", "ai-review-override"}:
        print(f"Unsupported repository dispatch command {command or '<missing>'}; skipping.")
        return outputs

    target_pr_number = env.get("DISPATCH_PR_NUMBER", "").strip()
    if not target_pr_number:
        raise ValueError("command dispatch requires pr_number")

    comment_author = env.get("DISPATCH_COMMENT_AUTHOR", "").strip()
    outputs["target_pr_number"] = target_pr_number
    outputs["comment_author"] = comment_author
    outputs["comment_id"] = env.get("DISPATCH_COMMENT_ID", "").strip()
    if command == "ai-review-breaker-reset":
        if event_action != "created":
            print("AI review breaker reset dispatch is not a created comment; skipping.")
            return outputs
        resolve_breaker_reset(
            body=body,
            comment_author=comment_author,
            outputs=outputs,
        )
    else:
        resolve_review_override(
            body=body,
            previous_body=env.get("DISPATCH_PREVIOUS_BODY", ""),
            event_action=event_action,
            comment_author=comment_author,
            comment_author_has_permission=comment_author_has_permission,
            approved_users=review_override_approvers(env),
            outputs=outputs,
        )
    return outputs


def resolve_pull_request_context(
    *,
    event: dict[str, Any],
    event_action: str,
    actor: str,
    outputs: dict[str, str],
) -> dict[str, str]:
    if event_action != "ready_for_review":
        print(f"Pull request event action {event_action} is not review-triggering; skipping.")
        return outputs

    outputs["should_run"] = "true"
    outputs["check_required"] = "true"
    outputs["run_codex"] = "true"
    outputs["target_pr_number"] = str(event["pull_request"]["number"])
    outputs["comment_author"] = actor
    return outputs


def resolve_review_context(
    *,
    env: dict[str, str],
    event: dict[str, Any],
    run_gh: GhOutput = gh_output,
) -> dict[str, str]:
    event_name = env["EVENT_NAME"]
    event_action = env.get("EVENT_ACTION", "")
    repository = env["REPOSITORY"]
    actor = env["ACTOR"]
    outputs = default_context_outputs(env)

    authorization_actor = actor
    if event_name == "repository_dispatch":
        authorization_actor = env.get("DISPATCH_COMMENT_AUTHOR", "").strip()

    dispatch_action = env.get("DISPATCH_EVENT_ACTION", "").strip()
    previous_override = is_review_override_command(env.get("DISPATCH_PREVIOUS_BODY", ""))
    is_override_revocation = (
        event_name == "repository_dispatch"
        and env.get("DISPATCH_COMMAND", "").strip() == "ai-review-override"
        and (dispatch_action == "deleted" or (dispatch_action == "edited" and previous_override))
    )

    authorized, permission = resolve_permission(
        event_name=event_name,
        repository=repository,
        actor=authorization_actor,
        run_gh=run_gh,
    )
    print(f"Resolved actor permission: {permission}")
    if not authorized and not is_override_revocation:
        print("Actor is not authorized for direct CLI review actions; workflow will skip.")
        return outputs

    if event_name == "workflow_dispatch":
        outputs = resolve_dispatch_context(env, outputs)
    elif event_name == "repository_dispatch":
        outputs = resolve_repository_dispatch_context(
            env,
            outputs,
            comment_author_has_permission=authorized,
        )
    elif event_name == "pull_request":
        outputs = resolve_pull_request_context(
            event=event,
            event_action=event_action,
            actor=actor,
            outputs=outputs,
        )
    else:
        print(f"Unsupported event combination: {event_name}/{event_action}; skipping.")
        return outputs

    return outputs
