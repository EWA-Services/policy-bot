"""Entrypoint for publishing redacted Kingfisher notifications."""

from __future__ import annotations

import os
from pathlib import Path

from secret_scan_report import load_findings, render_alert
from secret_scan_transport import notify_github_comment, notify_google_chat


def run_notifications() -> None:
    findings, report_valid = load_findings(Path(os.environ["REPORT_PATH"]))
    repository = os.environ.get("GITHUB_REPOSITORY", "unknown/unknown")
    run_url = os.environ.get("GITHUB_RUN_URL", "")
    github_server_url = os.environ.get("GITHUB_SERVER_URL", "")
    pr_url = os.environ.get("PR_URL", "")
    scan_outcome = os.environ.get("SCAN_OUTCOME", "unknown")
    if findings:
        alert_text, details, summary = render_alert(findings, repository, pr_url, run_url, github_server_url)
        notify_google_chat(alert_text)
    else:
        details, summary = [], ""
    notify_github_comment(
        details,
        summary,
        repository,
        run_url,
        scan_outcome,
        report_valid,
        findings,
        github_server_url,
    )


def main() -> int:
    """Never make alert delivery determine the scan result."""
    try:
        run_notifications()
    except Exception as error:
        print(f"::warning::Secret scan notifications skipped ({type(error).__name__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
