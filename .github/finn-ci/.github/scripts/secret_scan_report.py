"""Parse and format redacted Kingfisher findings."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from secret_scan_urls import require_github_web_url

COMMENT_MARKER = "<!-- ewa-secret-scan-report -->"
MAX_FINDINGS = 20
MAX_CHAT_PAYLOAD_BYTES = 32_000
CHAT_SIZE_NOTICE = "• Additional finding details omitted to fit Google Chat's message limit"
MAX_GITHUB_COMMENT_BYTES = 65_000
GITHUB_SIZE_NOTICE = "• Additional finding details omitted to fit GitHub's comment limit"
KINGFISHER_ACTIVE_STATUS = "Active Credential"
KINGFISHER_INACTIVE_STATUS = "Inactive Credential"
WITHHELD_COMMIT_MESSAGE = "Commit subject withheld to preserve report redaction"
WITHHELD_COMMITTER = "Committer withheld to preserve report redaction"
WITHHELD_COMMIT_DATE = "Commit date withheld to preserve report redaction"
WITHHELD_LOCATION = "File location withheld to preserve report redaction"


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def clean_text(value: Any, fallback: str = "unknown") -> str:
    text = " ".join(str(value or "").split())
    return text.replace("`", "'").replace("|", r"\|") or fallback


def chat_text(value: Any, fallback: str = "unknown") -> str:
    """Escape Chat markup, including user and link mentions."""
    return clean_text(value, fallback).replace("<", "&lt;").replace(">", "&gt;")


def github_text(value: Any, fallback: str = "unknown") -> str:
    """Escape mentions and link syntax in PR-controlled Markdown fields."""
    return (
        clean_text(value, fallback)
        .replace("\\", r"\\")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("://", ":\u200b//")
        .replace("@", "@\u200b")
        .replace("[", r"\[")
        .replace("]", r"\]")
        .replace("(", r"\(")
        .replace(")", r"\)")
        .replace("*", r"\*")
        .replace("_", r"\_")
        .replace("~", r"\~")
    )


def truncate_utf8(value: str, max_bytes: int, suffix: str = "…") -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    suffix_bytes = suffix.encode("utf-8")
    if max_bytes <= len(suffix_bytes):
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(suffix_bytes)].decode("utf-8", errors="ignore")
    return prefix + suffix


def encode_chat_payload(message: str) -> bytes:
    """Encode a compact Google Chat webhook payload."""
    return json.dumps({"text": message}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_bounded_message(
    header_lines: list[str],
    detail_blocks: list[str],
    footer: str,
    max_bytes: int,
    size_notice: str,
    measure_bytes: Callable[[str], int] | None = None,
) -> str:
    """Assemble a bounded notification while preserving key context."""
    header_lines = [truncate_utf8(line, 4_096) for line in header_lines]
    footer = truncate_utf8(footer, 4_096)
    lines = list(header_lines)
    measure_bytes = measure_bytes or (lambda value: len(value.encode("utf-8")))

    def fits(candidate_lines: list[str]) -> bool:
        candidate = "\n".join([*candidate_lines, "", footer])
        return measure_bytes(candidate) <= max_bytes

    if not fits(lines):
        raise ValueError("Notification header and footer exceed the platform limit")

    for index, detail_block in enumerate(detail_blocks):
        has_more_details = index < len(detail_blocks) - 1
        if has_more_details and not fits([*lines, detail_block, size_notice]):
            if fits([*lines, size_notice]):
                lines.append(size_notice)
            break
        if fits([*lines, detail_block]):
            lines.append(detail_block)
            continue
        if fits([*lines, size_notice]):
            lines.append(size_notice)
        break

    return "\n".join([*lines, "", footer])


def build_bounded_chat_message(header_lines: list[str], detail_blocks: list[str], footer: str) -> str:
    """Keep the Chat payload within its byte limit."""
    return build_bounded_message(
        header_lines,
        detail_blocks,
        footer,
        MAX_CHAT_PAYLOAD_BYTES,
        CHAT_SIZE_NOTICE,
        lambda message: len(encode_chat_payload(message)),
    )


def build_bounded_github_comment(header_lines: list[str], detail_blocks: list[str], footer: str) -> str:
    """Keep the GitHub comment below the platform size limit."""
    return build_bounded_message(
        header_lines,
        detail_blocks,
        footer,
        MAX_GITHUB_COMMENT_BYTES,
        GITHUB_SIZE_NOTICE,
    )


def load_findings(report_path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Return findings and whether the report was successfully parsed."""
    try:
        raw_report_text = report_path.read_text(encoding="utf-8")
    except OSError:
        return [], False
    if raw_report_text == "":
        return [], True
    report_text = raw_report_text.strip()
    if not report_text:
        return [], False

    try:
        report: Any = json.loads(report_text)
    except json.JSONDecodeError:
        try:
            report = [json.loads(line) for line in report_text.splitlines() if line.strip()]
        except json.JSONDecodeError:
            return [], False

    records = report if isinstance(report, list) else [report]
    if not records:
        return [], False

    raw_findings = []
    for record in records:
        if not isinstance(record, dict):
            return [], False
        if "finding" in record:
            raw_findings.append(record)
            continue
        if "findings" in record:
            envelope_findings = record["findings"]
            if not isinstance(envelope_findings, list):
                return [], False
            raw_findings.extend(envelope_findings)
            continue
        if set(record) == {"access_map"}:
            continue
        return [], False

    if any(not isinstance(finding, dict) for finding in raw_findings):
        return [], False
    return raw_findings, True


def finding_status(finding: dict[str, Any]) -> str:
    validation = finding.get("validation", "")
    validation = mapping(validation).get("status", validation)
    if validation == KINGFISHER_INACTIVE_STATUS:
        return "not confirmed active"
    if validation == KINGFISHER_ACTIVE_STATUS:
        return "confirmed active"
    return "not validated"


def commit_context() -> tuple[str, str, str]:
    """Withhold unredacted Git metadata from outbound notifications."""
    return (
        WITHHELD_COMMIT_MESSAGE,
        WITHHELD_COMMITTER,
        WITHHELD_COMMIT_DATE,
    )


def safe_commit_id(value: Any) -> str:
    """Return only hash-shaped commit identifiers from report metadata."""
    commit_id = str(value or "")
    if not re.fullmatch(r"[0-9a-fA-F]{12,64}", commit_id):
        return "unknown"
    return commit_id[:12].lower()


def finding_details(record: dict[str, Any]) -> dict[str, str]:
    finding = mapping(record.get("finding"))
    rule = mapping(record.get("rule") or finding.get("rule"))
    git_metadata = mapping(finding.get("git_metadata"))
    commit = mapping(git_metadata.get("commit"))
    commit_id = safe_commit_id(commit.get("id"))
    message, committer, commit_date = commit_context()

    return {
        "commit": commit_id,
        "message": message,
        "rule": clean_text(rule.get("name") or rule.get("id")),
        "location": WITHHELD_LOCATION,
        "committer": committer,
        "date": commit_date,
        "status": finding_status(finding),
    }


def render_findings(
    findings: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], str]:
    statuses = [finding_status(mapping(record.get("finding"))) for record in findings]
    details = [finding_details(record) for record in findings[:MAX_FINDINGS]]
    active_count = statuses.count("confirmed active")
    not_validated_count = statuses.count("not validated")
    not_active_count = len(statuses) - active_count - not_validated_count
    summary = (
        f"Confirmed active: {active_count} · "
        f"Not confirmed active: {not_active_count} · "
        f"Not validated: {not_validated_count}"
    )
    return details, summary


def render_alert(
    findings: list[dict[str, Any]],
    repository: str,
    pr_url: str,
    run_url: str,
    github_server_url: str,
) -> tuple[str, list[dict[str, str]], str]:
    safe_run_url = require_github_web_url(
        run_url,
        github_server_url,
        repository,
        r"actions/runs/[0-9]+",
        "GitHub run URL",
    )
    safe_pr_url = ""
    if pr_url:
        safe_pr_url = require_github_web_url(
            pr_url,
            github_server_url,
            repository,
            r"pull/[0-9]+",
            "GitHub pull request URL",
        )
    details, summary = render_findings(findings)
    header_lines = [
        f"🚨 Secret scan findings in {chat_text(repository)} ({len(findings)} potential finding(s))",
        summary,
    ]
    if safe_pr_url:
        header_lines.append(f"Pull request: {safe_pr_url}")
    detail_blocks = []
    for detail in details[:MAX_FINDINGS]:
        detail_blocks.append(
            "\n".join(
                [
                    f"• `{chat_text(detail['commit'])}` {chat_text(detail['message'])}",
                    (
                        f"  `{chat_text(detail['rule'])}` · `{chat_text(detail['location'])}` · "
                        f"committer: `{chat_text(detail['committer'])}` · "
                        f"`{chat_text(detail['date'])}`"
                    ),
                    f"  Status: {chat_text(detail['status'])}",
                ]
            )
        )
    if len(findings) > MAX_FINDINGS:
        detail_blocks.append(f"• {len(findings) - MAX_FINDINGS} additional finding(s) omitted")
    message = build_bounded_chat_message(
        header_lines,
        detail_blocks,
        f"Report: {safe_run_url} (artifact: secret-scan-report)",
    )
    return message, details, summary


def build_github_comment(
    details: list[dict[str, str]],
    summary: str,
    repository: str,
    run_url: str,
    scan_outcome: str,
    report_valid: bool,
    github_server_url: str,
    total_findings: int | None = None,
    has_active_findings: bool | None = None,
) -> str | None:
    safe_run_url = require_github_web_url(
        run_url,
        github_server_url,
        repository,
        r"actions/runs/[0-9]+",
        "GitHub run URL",
    )
    if details:
        finding_count = len(details) if total_findings is None else total_findings
        active_count = (
            any(detail["status"] == "confirmed active" for detail in details)
            if has_active_findings is None
            else has_active_findings
        )
        validation_note = (
            "Values are redacted; review the validation summary below."
            if active_count
            else "Values are redacted; validation did not confirm any credentials are active."
        )
        header_lines = [
            COMMENT_MARKER,
            f"## 🚨 Secret scan: {finding_count} potential finding(s)",
            "",
            "The changed files contain potential secrets. " + validation_note,
            "",
            f"Repository: `{github_text(repository)}`",
            f"Summary: {summary}",
            "",
        ]
        detail_blocks = []
        for detail in details[:MAX_FINDINGS]:
            detail_blocks.append(
                "\n".join(
                    [
                        f"- `{github_text(detail['commit'])}` `{github_text(detail['message'])}`",
                        (
                            f"  - `{github_text(detail['rule'])}` · "
                            f"`{github_text(detail['location'])}` · "
                            f"`{github_text(detail['committer'])}` · "
                            f"`{github_text(detail['date'])}` · "
                            f"**{github_text(detail['status'])}**"
                        ),
                    ]
                )
            )
        if finding_count > MAX_FINDINGS:
            detail_blocks.append(f"- {finding_count - MAX_FINDINGS} additional finding(s) omitted")
        return build_bounded_github_comment(
            header_lines,
            detail_blocks,
            f"[View the workflow run and redacted report]({safe_run_url})",
        )

    if scan_outcome != "success" or not report_valid:
        return None
    return build_bounded_github_comment(
        [
            COMMENT_MARKER,
            "## ✅ Secret scan passed",
            "",
            (
                "No potential secrets were found in the changed files. "
                "This comment is updated on reruns to avoid notification noise."
            ),
            "",
            f"Repository: `{github_text(repository)}`",
        ],
        [],
        f"[View the workflow run]({safe_run_url})",
    )
