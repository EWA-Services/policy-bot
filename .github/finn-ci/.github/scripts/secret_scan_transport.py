"""Secure HTTP transport and notification delivery for secret-scan alerts."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from secret_scan_report import (
    COMMENT_MARKER,
    MAX_CHAT_PAYLOAD_BYTES,
    build_github_comment,
    encode_chat_payload,
    finding_status,
    mapping,
)
from secret_scan_urls import (
    require_https_url,
    require_pull_request_number,
    require_same_https_origin,
)

GITHUB_ACTIONS_BOT_LOGIN = "github-actions[bot]"
CHAT_MAX_ATTEMPTS = 3
CHAT_MAX_RETRY_DELAY_SECONDS = 5.0
CHAT_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_handle: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        require_same_https_origin(new_url, request.full_url, "Redirect target")
        return super().redirect_request(request, file_handle, code, message, headers, new_url)


https_opener = urllib.request.build_opener(HttpsOnlyRedirectHandler)


def github_request(
    method: str,
    url: str,
    token: str,
    api_origin: str,
    body: dict[str, str] | None = None,
) -> tuple[Any, Any]:
    require_same_https_origin(url, api_origin, "GitHub URL")
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    with https_opener.open(request, timeout=20) as response:
        response_text = response.read().decode("utf-8")
        payload = json.loads(response_text) if response_text else {}
        return payload, response.headers


def next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for link in link_header.split(","):
        match = re.search(r"<([^>]+)>;\s*rel=\"next\"", link)
        if match:
            return match.group(1)
    return None


def list_github_comments(comments_url: str, token: str, api_origin: str) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    url = f"{comments_url}?per_page=100"
    while url:
        payload, headers = github_request("GET", url, token, api_origin)
        if isinstance(payload, list):
            comments.extend(comment for comment in payload if isinstance(comment, dict))
        url = next_link(headers.get("Link"))
        if url:
            require_same_https_origin(url, api_origin, "GitHub pagination URL")
    return comments


def owned_marker_comments(comments: list[dict[str, Any]], comment_author: str) -> list[dict[str, Any]]:
    """Return this action's comments in stable oldest-first order."""
    if not comment_author or any(character.isspace() for character in comment_author):
        raise ValueError("GitHub comment author must be a non-empty login")
    marker_comments = [
        comment
        for comment in comments
        if isinstance(comment.get("body"), str)
        and (comment["body"] == COMMENT_MARKER or comment["body"].startswith(f"{COMMENT_MARKER}\n"))
        and isinstance(comment.get("user"), dict)
        and comment["user"].get("login") == comment_author
    ]
    return sorted(
        marker_comments,
        key=lambda comment: (
            not isinstance(comment.get("id"), int),
            comment.get("id") if isinstance(comment.get("id"), int) else 0,
        ),
    )


def update_primary_comment(
    marker_comments: list[dict[str, Any]],
    token: str,
    api_origin: str,
    comment_body: str,
) -> None:
    """Update the oldest marker comment and remove any duplicates."""
    primary_url = require_same_https_origin(marker_comments[0]["url"], api_origin, "GitHub comment URL")
    github_request("PATCH", primary_url, token, api_origin, {"body": comment_body})
    for duplicate in marker_comments[1:]:
        duplicate_url = require_same_https_origin(duplicate["url"], api_origin, "GitHub duplicate comment URL")
        try:
            github_request("DELETE", duplicate_url, token, api_origin)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                print(f"::warning::GitHub duplicate secret scan comment cleanup failed (HTTP {error.code})")
        except Exception as error:
            print(f"::warning::GitHub duplicate secret scan comment cleanup failed ({type(error).__name__})")


def upsert_github_comment(
    comments_url: str,
    token: str,
    comment_body: str,
    create_if_missing: bool = True,
    api_origin: str | None = None,
    comment_author: str = GITHUB_ACTIONS_BOT_LOGIN,
) -> str:
    api_origin = api_origin or comments_url
    require_same_https_origin(comments_url, api_origin, "GitHub comments URL")
    comments = list_github_comments(comments_url, token, api_origin)
    marker_comments = owned_marker_comments(comments, comment_author)
    if marker_comments:
        update_primary_comment(marker_comments, token, api_origin, comment_body)
        return "updated"
    if not create_if_missing:
        return "skipped"
    github_request("POST", comments_url, token, api_origin, {"body": comment_body})
    reconciled_comments = owned_marker_comments(list_github_comments(comments_url, token, api_origin), comment_author)
    if reconciled_comments:
        update_primary_comment(reconciled_comments, token, api_origin, comment_body)
    return "created"


def retry_after_seconds(headers: Any, attempt: int) -> float:
    """Return a bounded Retry-After delay or exponential fallback."""
    fallback = min(2 ** (attempt - 1), CHAT_MAX_RETRY_DELAY_SECONDS)
    retry_after = headers.get("Retry-After") if headers else None
    if not retry_after:
        return fallback
    try:
        delay = float(retry_after)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(retry_after))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return fallback
    return min(max(delay, 0.0), CHAT_MAX_RETRY_DELAY_SECONDS)


def post_chat_alert(webhook: str, message: str) -> None:
    require_https_url(webhook, "Google Chat webhook")
    payload = encode_chat_payload(message)
    if len(payload) > MAX_CHAT_PAYLOAD_BYTES:
        raise ValueError("Google Chat payload exceeds the platform limit")
    for attempt in range(1, CHAT_MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with https_opener.open(request, timeout=20):
                return
        except urllib.error.HTTPError as error:
            if error.code not in CHAT_RETRYABLE_STATUS_CODES or attempt == CHAT_MAX_ATTEMPTS:
                raise
            time.sleep(retry_after_seconds(error.headers, attempt))


def notify_google_chat(alert_text: str) -> None:
    webhook = os.environ.get("GOOGLE_CHAT_SECURITY_SCAN_SECRET_WEBHOOK", "")
    if not webhook:
        print("Google Chat security alerting is not configured")
        return
    try:
        post_chat_alert(webhook, alert_text)
        print("Posted secret scan alert to Google Chat")
    except urllib.error.HTTPError as error:
        print(f"::warning::Google Chat alert failed with HTTP {error.code}")
    except Exception as error:
        print(f"::warning::Google Chat alert failed ({type(error).__name__})")


def notify_github_comment(
    details: list[dict[str, str]],
    summary: str,
    repository: str,
    run_url: str,
    scan_outcome: str,
    report_valid: bool,
    findings: list[dict[str, Any]],
    github_server_url: str,
) -> None:
    pr_number = os.environ.get("PR_NUMBER", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not pr_number or not github_token:
        if findings:
            print("GitHub secret scan comments are not available in this event")
        return

    try:
        api_url = require_https_url(os.environ["GITHUB_API_URL"].rstrip("/"), "GitHub API URL")
        api_origin = api_url
        safe_pr_number = require_pull_request_number(pr_number)
        comments_url = f"{api_url}/repos/{repository}/issues/{safe_pr_number}/comments"
        comment_body = build_github_comment(
            details,
            summary,
            repository,
            run_url,
            scan_outcome,
            report_valid,
            github_server_url,
            total_findings=len(findings),
            has_active_findings=any(
                finding_status(mapping(record.get("finding"))) == "confirmed active" for record in findings
            ),
        )
        if comment_body is None:
            print("No GitHub secret scan comment update: the report is incomplete or the scan did not succeed")
            return
        result = upsert_github_comment(
            comments_url,
            github_token,
            comment_body,
            create_if_missing=bool(details),
            api_origin=api_origin,
            comment_author=os.environ.get("GITHUB_COMMENT_AUTHOR", GITHUB_ACTIONS_BOT_LOGIN),
        )
        print(f"{result.capitalize()} the reusable secret scan GitHub comment")
    except urllib.error.HTTPError as error:
        print(f"::warning::GitHub secret scan comment failed with HTTP {error.code}")
    except Exception as error:
        print(f"::warning::GitHub secret scan comment failed ({type(error).__name__})")
