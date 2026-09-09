#!/usr/bin/env python3
"""Strict structured-output schemas for the Codex review, breaker, and admission calls.

Every object declares additionalProperties=false and lists every property in
required; the Responses API rejects strict schemas that do not.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from code_review_findings import (
        MAX_FINDING_TEXT_LENGTH,
        MAX_OUTPUT_TEXT_LENGTH,
        NO_COVERING_RULE,
        VERDICT_APPROVE,
        VERDICT_REQUEST_CHANGES,
        VERDICT_WAIT,
    )
else:
    from .code_review_findings import (
        MAX_FINDING_TEXT_LENGTH,
        MAX_OUTPUT_TEXT_LENGTH,
        NO_COVERING_RULE,
        VERDICT_APPROVE,
        VERDICT_REQUEST_CHANGES,
        VERDICT_WAIT,
    )


def _schema_string(*, max_length: int) -> dict[str, object]:
    return {"type": "string", "minLength": 1, "maxLength": max_length}


def _strict_object(properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def review_output_schema() -> dict[str, object]:
    # The published verdictReason is rendered from findings, so it is not requested here.
    finding_text = _schema_string(max_length=MAX_FINDING_TEXT_LENGTH)
    return _strict_object(
        {
            "verdict": {"type": "string", "enum": [VERDICT_APPROVE, VERDICT_REQUEST_CHANGES, VERDICT_WAIT]},
            "findings": {
                "type": "array",
                "items": _strict_object(
                    {
                        "rule": {
                            **_schema_string(max_length=200),
                            "description": (
                                "Exact rule id from the installed finn-coding-standards skill, or "
                                f"`{NO_COVERING_RULE}` when the finding is a real defect that no installed rule "
                                "covers. Never invent a rule id."
                            ),
                        },
                        "location": finding_text,
                        "requirement": finding_text,
                        "supportedAction": finding_text,
                        "incorrectResult": finding_text,
                    }
                ),
            },
            "observations": {
                "type": "array",
                "items": _strict_object({"note": finding_text}),
            },
        }
    )


def breaker_analysis_output_schema() -> dict[str, object]:
    text = _schema_string(max_length=MAX_OUTPUT_TEXT_LENGTH)
    return _strict_object({"explanation": text, "recommendedNextAction": text})


def shadow_admission_output_schema() -> dict[str, object]:
    return _strict_object(
        {
            "admittedFindings": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
                "description": (
                    "Zero-based index of every reviewed finding that meets the proof standard; empty when none does."
                ),
            },
            "reason": {
                **_schema_string(max_length=MAX_OUTPUT_TEXT_LENGTH),
                "description": "Why the non-admitted findings fail the proof standard, or why nothing was admitted.",
            },
        }
    )
