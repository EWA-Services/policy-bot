#!/usr/bin/env python3
"""Parse the skill decision and project it to the trusted GitHub publisher."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from code_review_findings import (
        FIELD_FINDINGS,
        FIELD_OBSERVATIONS,
        FIELD_REASON,
        FIELD_RULE,
        FIELD_VERDICT,
        FIELD_VERDICT_REASON,
        MAX_OUTPUT_TEXT_LENGTH,
        SUPPORTED_REVIEW_VERDICTS,
        VERDICT_APPROVE,
        VERDICT_REQUEST_CHANGES,
        VERDICT_WAIT,
        FilteredFindings,
        _contains_unsafe_markup,
        filter_review_findings,
        observation_notes,
        render_verdict_reason,
    )
else:
    from .code_review_findings import (
        FIELD_FINDINGS,
        FIELD_OBSERVATIONS,
        FIELD_REASON,
        FIELD_RULE,
        FIELD_VERDICT,
        FIELD_VERDICT_REASON,
        MAX_OUTPUT_TEXT_LENGTH,
        SUPPORTED_REVIEW_VERDICTS,
        VERDICT_APPROVE,
        VERDICT_REQUEST_CHANGES,
        VERDICT_WAIT,
        FilteredFindings,
        _contains_unsafe_markup,
        filter_review_findings,
        observation_notes,
        render_verdict_reason,
    )

ALLOWED_REVIEW_FIELDS = frozenset({FIELD_VERDICT, FIELD_VERDICT_REASON, FIELD_FINDINGS, FIELD_OBSERVATIONS})
FIELD_ADMITTED_FINDINGS = "admittedFindings"
EXPECTED_SHADOW_ADMISSION_FIELDS = frozenset({FIELD_ADMITTED_FINDINGS, FIELD_REASON})
FAILURE_TITLE = "AI review failed"
REVIEW_CONTRACT_ERROR = "Codex review output fields do not match the published result contract"
ADMISSION_CONTRACT_ERROR = "Shadow admission output fields do not match the enforcing contract"
VERDICT_FINDINGS_MISMATCH_ERROR = "Codex review verdict does not match its blocking findings"
WAIT_WITHOUT_EVIDENCE_ERROR = "Codex review WAIT must name the missing evidence in observations"


def structured_output_text(value: dict[str, Any], field: str) -> str:
    text = value.get(field)
    if not isinstance(text, str):
        raise RuntimeError(f"{field} must be a string")
    text = text.strip()
    if not text:
        raise RuntimeError(f"{field} must not be empty")
    if _contains_unsafe_markup(text):
        raise RuntimeError(f"{field} must not contain NUL bytes or HTML comments")
    if len(text) > MAX_OUTPUT_TEXT_LENGTH:
        raise RuntimeError(f"{field} must not exceed {MAX_OUTPUT_TEXT_LENGTH} characters")
    return text


def verdict_reason_text(review: dict[str, Any]) -> str:
    return structured_output_text(review, FIELD_VERDICT_REASON)


def encode_verdict_reason(reason: str) -> str:
    return base64.b64encode(reason.encode("utf-8")).decode("ascii")


def decode_verdict_reason(encoded_reason: str) -> str:
    try:
        reason = base64.b64decode(encoded_reason, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise RuntimeError("Encoded verdict reason is invalid") from error
    return structured_output_text({FIELD_VERDICT_REASON: reason}, FIELD_VERDICT_REASON)


def append_step_summary(title: str, body: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write(f"\n{title}\n\n{body}\n")


def _require_verdict_matches_findings(verdict: str, kept: list[dict[str, str]]) -> None:
    """Fail closed on inconsistent model output instead of repairing it into a verdict."""
    if bool(kept) != (verdict == VERDICT_REQUEST_CHANGES):
        raise RuntimeError(f"{VERDICT_FINDINGS_MISMATCH_ERROR}: {verdict} with {len(kept)} blocking finding(s)")


def _require_review_object(review: object) -> dict[str, Any]:
    if not isinstance(review, dict):
        raise RuntimeError("Codex review output must be a JSON object")
    extra = set(review) - ALLOWED_REVIEW_FIELDS
    if extra or FIELD_VERDICT not in review:
        raise RuntimeError(REVIEW_CONTRACT_ERROR)
    if FIELD_FINDINGS not in review and FIELD_VERDICT_REASON not in review:
        raise RuntimeError(REVIEW_CONTRACT_ERROR)
    return review


def parse_review_result(
    review: object,
    *,
    known_rule_ids: frozenset[str] | None = None,
) -> dict[str, str]:
    review = _require_review_object(review)
    verdict = review.get(FIELD_VERDICT)
    if verdict not in SUPPORTED_REVIEW_VERDICTS:
        raise RuntimeError(f"Unsupported verdict value: {verdict} (expected APPROVE|REQUEST_CHANGES|WAIT).")

    filtered = FilteredFindings([], [])
    if FIELD_FINDINGS in review:
        filtered = filter_review_findings(review.get(FIELD_FINDINGS), known_rule_ids=known_rule_ids)
        _require_verdict_matches_findings(verdict, filtered.kept)
        observations = observation_notes(review.get(FIELD_OBSERVATIONS))
        if verdict == VERDICT_WAIT and not observations:
            raise RuntimeError(WAIT_WITHOUT_EVIDENCE_ERROR)
        reason = render_verdict_reason(verdict, filtered.kept, observations=observations)
    else:
        reason = verdict_reason_text(review)

    findings_json = _compact_json(filtered.kept)
    return {
        "full_review_performed": "true",
        FIELD_VERDICT: verdict,
        "verdict_reason_base64": encode_verdict_reason(reason),
        "findings_json": findings_json,
        # Base64 keeps the cross-job transport opaque to GitHub's secret masking,
        # which silently omits job outputs that resemble a secret.
        "findings_base64": base64.b64encode(findings_json.encode("utf-8")).decode("ascii"),
        "downgraded_findings_json": _compact_json(filtered.downgraded),
    }


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def load_trusted_findings(findings_base64: str) -> list[dict[str, str]]:
    """Decode the filtered findings the review job published alongside its reason."""
    if not findings_base64.strip():
        return []
    try:
        findings = json.loads(base64.b64decode(findings_base64, validate=True).decode("utf-8"))
    except ValueError as exc:
        raise RuntimeError("Trusted review findings must be base64-encoded JSON") from exc
    if not isinstance(findings, list) or not all(
        isinstance(item, dict) and isinstance(item.get(FIELD_RULE), str) for item in findings
    ):
        raise RuntimeError("Trusted review findings must be a list of findings with a rule id")
    return findings


def _is_index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_shadow_admission_result(value: object, finding_count: int) -> dict[str, Any]:
    """Validate the judge's per-finding decisions against the findings it was given."""
    if not isinstance(value, dict):
        raise RuntimeError("Shadow admission output must be a JSON object")
    if set(value) != EXPECTED_SHADOW_ADMISSION_FIELDS:
        raise RuntimeError(ADMISSION_CONTRACT_ERROR)
    admitted = value[FIELD_ADMITTED_FINDINGS]
    if not isinstance(admitted, list) or not all(_is_index(index) for index in admitted):
        raise RuntimeError(ADMISSION_CONTRACT_ERROR)
    if any(index < 0 or index >= finding_count for index in admitted):
        raise RuntimeError(f"Shadow admission admitted a finding index outside 0..{finding_count - 1}")
    return {
        FIELD_ADMITTED_FINDINGS: sorted(set(admitted)),
        FIELD_REASON: structured_output_text(value, FIELD_REASON),
    }


def resolve_admission(
    judged: dict[str, Any],
    findings: list[dict[str, str]],
    rule_ids: frozenset[str],
) -> dict[str, Any]:
    """Combine the judge's admitted findings with the ones an installed rule protects.

    Protection is decided only by each structured finding's rule: an installed rule id
    protects it, NO_COVERING_RULE does not, and rule ids mentioned in finding text confer
    nothing. The published reason is rendered from the admitted findings, so the judge's
    free text cannot drop a protected finding or add an unjudged one.
    """
    protected = [index for index, finding in enumerate(findings) if finding[FIELD_RULE] in rule_ids]
    admitted = sorted(set(judged[FIELD_ADMITTED_FINDINGS]) | set(protected))
    if not admitted:
        return {
            "classification": "REJECT",
            FIELD_REASON: judged[FIELD_REASON],
            FIELD_ADMITTED_FINDINGS: [],
            "protectedFindings": protected,
        }
    return {
        "classification": "ADMIT",
        FIELD_REASON: render_verdict_reason(VERDICT_REQUEST_CHANGES, [findings[index] for index in admitted]),
        FIELD_ADMITTED_FINDINGS: admitted,
        "protectedFindings": protected,
    }


def failed_review(summary: str, *, provider: str) -> dict[str, str]:
    return {
        "final_verdict": "error",
        "title": FAILURE_TITLE,
        "summary": summary,
        "provider": provider,
    }


def resolve_override(actor: str) -> dict[str, str]:
    """Publish an override actor authorized by the workflow context stage."""
    return {
        "final_verdict": "approved",
        "title": "AI review override approved",
        "summary": f"AI review overridden by @{actor} for the current pull request head.",
        "provider": "human-override",
    }


def resolve_codex_outcome(
    *,
    result: str,
    verdict: str,
    reason: str,
    full_review: str,
    admission_result: str = "",
    admission_classification: str = "",
    admission_reason: str = "",
) -> dict[str, str]:
    if result == "cancelled":
        return {
            "final_verdict": "skipped",
            "title": "AI review check unchanged",
            "summary": "The cancelled Codex job left the finn-ai-coder check unchanged.",
            "provider": "codex",
        }
    if result != "success":
        return failed_review(f"Codex job ended with {result or 'unknown'}.", provider="codex")
    if full_review != "true":
        return failed_review("Codex did not return a complete review decision.", provider="codex")

    decisions = {
        VERDICT_APPROVE: ("approved", "AI review approved"),
        VERDICT_WAIT: ("none", "AI review waiting for evidence"),
    }
    if verdict == VERDICT_REQUEST_CHANGES:
        original = {
            "final_verdict": "requested_changes",
            "title": "AI review requested changes",
            "summary": reason,
            "provider": "codex",
        }
        if admission_result != "success" or admission_classification not in {"ADMIT", "REJECT"}:
            return original
        try:
            filtered_reason = structured_output_text({FIELD_REASON: admission_reason}, FIELD_REASON)
        except RuntimeError:
            return original
        if admission_classification == "REJECT":
            return {
                "final_verdict": "approved",
                "title": "AI review approved",
                "summary": filtered_reason,
                "provider": "codex",
            }
        return {**original, "summary": filtered_reason}

    decision = decisions.get(verdict)
    if decision is None:
        return failed_review(f"Codex returned unsupported verdict {verdict or 'empty'}.", provider="codex")
    final_verdict, title = decision
    return {
        "final_verdict": final_verdict,
        "title": title,
        "summary": reason,
        "provider": "codex",
    }


def resolve_final_verdict(
    *,
    check_required: str,
    start_result: str,
    run_codex: str,
    codex_result: str,
    codex_verdict: str,
    codex_reason: str,
    codex_full_review: str,
    review_override_actor: str = "",
    admission_result: str = "",
    admission_classification: str = "",
    admission_reason: str = "",
) -> dict[str, str]:
    if check_required != "true":
        return {
            "final_verdict": "skipped",
            "title": "AI review check unchanged",
            "summary": "This workflow run did not require a full pull request review.",
            "provider": "unknown",
        }
    if start_result != "success":
        return failed_review(f"Review target preparation ended with {start_result or 'unknown'}.", provider="unknown")
    if review_override_actor:
        return resolve_override(review_override_actor)
    if run_codex != "true":
        return {
            "final_verdict": "none",
            "title": "AI review required",
            "summary": "No reviewer ran for the current pull request head.",
            "provider": "unknown",
        }
    return resolve_codex_outcome(
        result=codex_result,
        verdict=codex_verdict,
        reason=codex_reason,
        full_review=codex_full_review,
        admission_result=admission_result,
        admission_classification=admission_classification,
        admission_reason=admission_reason,
    )
