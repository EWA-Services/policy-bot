#!/usr/bin/env python3
"""CLI dispatcher for the direct CLI code-review workflow."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.code_review.code_review_context import resolve_review_context
from scripts.code_review.code_review_results import (
    append_step_summary,
    decode_verdict_reason,
    parse_review_result,
    resolve_final_verdict,
)
from scripts.code_review_alerts import (
    CODEX_AUTH_RUNBOOK_LABEL,
    CODEX_AUTH_RUNBOOK_URL,
    CodexAuthAlertContext,
    CodexAuthAlertResult,
    is_codex_auth_failure,
    notify_codex_auth_failure,
    result_outputs,
)
from scripts.code_review_common import output_values

_TEXT_ENCODING = "utf-8"


def command_resolve_context(_args: argparse.Namespace) -> int:  # pragma: no cover
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding=_TEXT_ENCODING))
    output_values(resolve_review_context(env=dict(os.environ), event=event))
    return 0


def command_parse_review_result(_args: argparse.Namespace) -> int:
    review = json.loads(Path("codex-output.txt").read_text(encoding=_TEXT_ENCODING))
    parsed = parse_review_result(review)
    rendered_review = json.dumps(review, indent=2, sort_keys=True) + "\n"
    Path("codex-review.json").write_text(  # NOSONAR - The workflow owns this fixed artifact path.
        rendered_review,
        encoding=_TEXT_ENCODING,
    )
    append_step_summary("Parsed Codex review:", f"```json\n{rendered_review}\n```")
    rendered_filtered = (
        json.dumps(
            {
                "downgraded_findings": json.loads(parsed["downgraded_findings_json"]),
                "findings": json.loads(parsed["findings_json"]),
                "verdict": parsed["verdict"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    Path("codex-review-filtered.json").write_text(  # NOSONAR - The workflow owns this fixed artifact path.
        rendered_filtered,
        encoding=_TEXT_ENCODING,
    )
    append_step_summary("Filtered review findings:", f"```json\n{rendered_filtered}\n```")
    output_values(parsed)
    return 0


def command_resolve_final_verdict(args: argparse.Namespace) -> int:  # pragma: no cover
    codex_reason = decode_verdict_reason(args.codex_reason_base64) if args.codex_reason_base64 else ""
    admission_reason = ""
    if args.codex_verdict == "REQUEST_CHANGES" and args.admission_result == "success" and args.admission_reason_base64:
        try:
            admission_reason = decode_verdict_reason(args.admission_reason_base64)
        except RuntimeError:
            pass
    output_values(
        resolve_final_verdict(
            check_required=args.check_required,
            start_result=args.start_result,
            run_codex=args.run_codex,
            codex_result=args.codex_result,
            codex_verdict=args.codex_verdict,
            codex_reason=codex_reason,
            codex_full_review=args.codex_full_review,
            review_override_actor=args.review_override_actor,
            admission_result=args.admission_result,
            admission_classification=args.admission_classification,
            admission_reason=admission_reason,
        )
    )
    return 0


def emit_codex_auth_error_annotation(secret_name: str) -> None:
    print(
        "::error title=Codex auth rotation required::"
        f"{secret_name} is invalidated or revoked. "
        f"Follow the {CODEX_AUTH_RUNBOOK_LABEL}: {CODEX_AUTH_RUNBOOK_URL}",
        file=sys.stderr,
    )


def command_notify_codex_auth_failure(args: argparse.Namespace) -> int:
    error_log = Path(args.error_log)
    try:
        result = notify_codex_auth_failure(
            error_log=error_log,
            context=CodexAuthAlertContext(
                repo=args.repo,
                pr_number=args.pr_number,
                run_url=args.run_url,
                job_name=args.job_name,
                model=args.model,
                secret_name=args.secret_name,
            ),
            webhook_url=os.environ.get("CODEX_AUTH_ALERT_WEBHOOK_URL", ""),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Codex auth alert failed: {exc}", file=sys.stderr)
        stderr = error_log.read_text(encoding=_TEXT_ENCODING, errors="replace") if error_log.exists() else ""
        detected = is_codex_auth_failure(stderr)
        result = CodexAuthAlertResult(detected=detected, notified=False, reason="alert_error")
        output_values(result_outputs(result))
        if detected:
            emit_codex_auth_error_annotation(args.secret_name)
        return 1

    output_values(result_outputs(result))
    if result.detected:
        emit_codex_auth_error_annotation(args.secret_name)
    return 0


def build_parser() -> argparse.ArgumentParser:  # pragma: no cover
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve-context")
    resolve.set_defaults(func=command_resolve_context)

    parse_result = subparsers.add_parser("parse-review-result")
    parse_result.set_defaults(func=command_parse_review_result)

    final_verdict = subparsers.add_parser("resolve-final-verdict")
    final_verdict.add_argument("--check-required", required=True)
    final_verdict.add_argument("--start-result", required=True)
    final_verdict.add_argument("--run-codex", required=True)
    final_verdict.add_argument("--codex-result", default="")
    final_verdict.add_argument("--codex-verdict", default="NONE")
    final_verdict.add_argument("--codex-reason-base64", default="")
    final_verdict.add_argument("--codex-full-review", default="false")
    final_verdict.add_argument("--review-override-actor", default="")
    final_verdict.add_argument("--admission-result", default="")
    final_verdict.add_argument("--admission-classification", default="")
    final_verdict.add_argument("--admission-reason-base64", default="")
    final_verdict.set_defaults(func=command_resolve_final_verdict)

    notify_auth = subparsers.add_parser("notify-codex-auth-failure")
    notify_auth.add_argument("--error-log", default="codex-error.txt")
    notify_auth.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    notify_auth.add_argument("--pr-number", default="")
    notify_auth.add_argument("--run-url", required=True)
    notify_auth.add_argument("--job-name", required=True)
    notify_auth.add_argument("--model", default="")
    notify_auth.add_argument("--secret-name", default="CODEX_AUTH_JSON")
    notify_auth.set_defaults(func=command_notify_codex_auth_failure)
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
