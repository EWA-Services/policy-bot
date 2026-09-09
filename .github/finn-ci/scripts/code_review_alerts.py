#!/usr/bin/env python3
"""Alert helpers for the direct CLI code-review workflow."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.code_review.code_review_github import gh_paginated_items, latest_marked_issue_comment
from scripts.code_review_common import GhOutput, gh_output

DEFAULT_SECRET_NAME = "CODEX_AUTH_JSON"
CODEX_AUTH_RUNBOOK_LABEL = "EWA-Actions Codex Code Review Auth runbook"
CODEX_AUTH_RUNBOOK_URL = (
    "https://app.notion.com/p/finn-app/EWA-Actions-Codex-Code-Review-Auth-37e032aabccd81a18689c768d0b64b1a"
)
AUTH_FAILURE_PATTERNS = (
    "refresh_token_invalidated",
    "refresh_token_reused",
    "token_invalidated",
    "authentication token has been invalidated",
    "refresh token was revoked",
    "refresh token was already used",
    "access token could not be refreshed",
)


@dataclass(frozen=True)
class CodexAuthAlertContext:
    repo: str
    pr_number: str
    run_url: str
    job_name: str
    model: str
    secret_name: str = DEFAULT_SECRET_NAME


@dataclass(frozen=True)
class CodexAuthAlertResult:
    detected: bool
    notified: bool
    reason: str
    pr_commented: bool = False


WebhookSender = Callable[[str, str], None]


def is_codex_auth_failure(stderr: str) -> bool:
    normalized = stderr.lower()
    return any(pattern in normalized for pattern in AUTH_FAILURE_PATTERNS)


def pr_comment_marker_name(secret_name: str = DEFAULT_SECRET_NAME) -> str:
    return f"codex-auth-alert-pr-comment:{secret_name}"


def pr_comment_marker(secret_name: str = DEFAULT_SECRET_NAME) -> str:
    return f"<!-- {pr_comment_marker_name(secret_name)} -->"


def chat_message(context: CodexAuthAlertContext) -> str:
    pr_line = f"*PR:* #{context.pr_number}\n" if context.pr_number else ""
    return (
        "*Codex auth rotation needed*\n\n"
        f"*Secret:* `{context.secret_name}`\n"
        f"*Detected in:* `{context.repo}`\n"
        f"{pr_line}"
        f"*Workflow:* `{context.job_name}`\n"
        f"*Run:* {context.run_url}\n\n"
        "Rotate the EWA-Services org secret and rerun the failed review."
    )


def pr_comment_body(context: CodexAuthAlertContext) -> str:
    return "\n".join(
        [
            pr_comment_marker(context.secret_name),
            "**Codex auth rotation required**",
            "",
            (
                "Codex could not start the direct CLI review because the shared authentication token is "
                "invalidated or revoked."
            ),
            "",
            f"- Secret: `{context.secret_name}`",
            f"- Workflow job: `{context.job_name}`",
            f"- Model: `{context.model or 'unknown'}`",
            f"- Run logs: {context.run_url}",
            f"- Runbook: [{CODEX_AUTH_RUNBOOK_LABEL}]({CODEX_AUTH_RUNBOOK_URL})",
            "",
            f"Follow the runbook to rotate `{context.secret_name}`, then rerun the code review workflow.",
        ]
    )


def send_google_chat_webhook(webhook_url: str, text: str) -> None:
    data = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 300:
                raise RuntimeError(f"Google Chat webhook returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Google Chat webhook returned HTTP {exc.code}: {body}") from exc


def latest_pr_auth_comment(
    *,
    context: CodexAuthAlertContext,
    bot_login: str,
    run_gh: GhOutput = gh_output,
) -> dict[str, Any] | None:
    if not context.pr_number:
        return None
    comments = gh_paginated_items(f"repos/{context.repo}/issues/{context.pr_number}/comments", run_gh=run_gh)
    return latest_marked_issue_comment(
        comments,
        bot_login=bot_login,
        marker=pr_comment_marker_name(context.secret_name),
    )


def post_or_update_pr_comment(
    *,
    context: CodexAuthAlertContext,
    run_gh: GhOutput = gh_output,
) -> bool:
    if not context.pr_number:
        return False

    body = pr_comment_body(context)
    bot_login = run_gh(["api", "user", "--jq", ".login"]).strip()
    if not bot_login:
        raise RuntimeError("GitHub API returned an empty authenticated login")
    existing = latest_pr_auth_comment(context=context, bot_login=bot_login, run_gh=run_gh)
    if existing is not None:
        run_gh(
            [
                "api",
                f"repos/{context.repo}/issues/comments/{existing['id']}",
                "--method",
                "PATCH",
                "-f",
                f"body={body}",
            ]
        )
        return True

    run_gh(
        [
            "api",
            f"repos/{context.repo}/issues/{context.pr_number}/comments",
            "--method",
            "POST",
            "-f",
            f"body={body}",
        ]
    )
    return True


def notify_codex_auth_failure(
    *,
    error_log: Path,
    context: CodexAuthAlertContext,
    webhook_url: str,
    run_gh: GhOutput = gh_output,
    send_webhook: WebhookSender = send_google_chat_webhook,
) -> CodexAuthAlertResult:
    stderr = error_log.read_text(encoding="utf-8", errors="replace") if error_log.exists() else ""
    if not is_codex_auth_failure(stderr):
        return CodexAuthAlertResult(
            detected=False,
            notified=False,
            reason="no_codex_auth_failure_signature",
        )

    pr_commented = post_or_update_pr_comment(context=context, run_gh=run_gh)
    if not webhook_url.strip():
        return CodexAuthAlertResult(
            detected=True,
            notified=False,
            reason="missing_webhook",
            pr_commented=pr_commented,
        )

    send_webhook(webhook_url, chat_message(context))
    return CodexAuthAlertResult(
        detected=True,
        notified=True,
        reason="alert_sent",
        pr_commented=pr_commented,
    )


def result_outputs(result: CodexAuthAlertResult) -> dict[str, Any]:
    return {
        "codex_auth_failure_detected": str(result.detected).lower(),
        "codex_auth_alert_notified": str(result.notified).lower(),
        "codex_auth_alert_reason": result.reason,
        "codex_auth_pr_comment_posted": str(result.pr_commented).lower(),
        "codex_auth_runbook_url": CODEX_AUTH_RUNBOOK_URL,
    }
