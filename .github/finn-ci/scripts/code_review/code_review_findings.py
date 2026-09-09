#!/usr/bin/env python3
"""Structured review findings: rule-index loading, deterministic filtering, and rendering."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import NamedTuple

VERDICT_APPROVE = "APPROVE"
VERDICT_REQUEST_CHANGES = "REQUEST_CHANGES"
VERDICT_WAIT = "WAIT"
SUPPORTED_REVIEW_VERDICTS = frozenset({VERDICT_APPROVE, VERDICT_REQUEST_CHANGES, VERDICT_WAIT})

FIELD_FINDINGS = "findings"
FIELD_FINDING = "finding"
FIELD_INDEX = "index"
FIELD_OBSERVATIONS = "observations"
FIELD_REASON = "reason"
FIELD_RULE = "rule"
FIELD_VERDICT = "verdict"
FIELD_VERDICT_REASON = "verdictReason"

UNKNOWN_RULE_REASON_SUFFIX = "is not an installed coding-standard rule"
# Sentinel rule id for a blocking finding that no installed coding-standards rule
# covers. It is kept and published, but it is not protected from an admission REJECT.
NO_COVERING_RULE = "no-covering-rule"
RULE_INDEX_UNAVAILABLE_ERROR = "finn-coding-standards rule index is unavailable; findings cannot be validated"
FINDING_REJECTED_ERROR = "Codex review finding cannot be published"
# Published review text and the base64 transport share this bound; rendering
# must stay inside it or decode_verdict_reason fails the workflow.
MAX_OUTPUT_TEXT_LENGTH = 4000
MAX_FINDING_TEXT_LENGTH = 900
_RULE_BULLET = re.compile(r"^- \*\*[^*]+\*\* \(`([^`]+)`\):", re.MULTILINE)
_FINDING_TEXT_FIELDS = (
    "location",
    "requirement",
    "supportedAction",
    "incorrectResult",
)
_FINDING_REASON_LABELS = {
    "location": "Location",
    "requirement": "Requirement",
    "supportedAction": "Supported action",
    "incorrectResult": "Incorrect result",
}


def _contains_unsafe_markup(text: str) -> bool:
    return "\x00" in text or "<!--" in text or "-->" in text


def coding_standards_skill_root(codex_home: str | Path) -> Path:
    """Return the installed finn-coding-standards skill path under Codex home."""
    return Path(codex_home) / "skills" / "finn-coding-standards"


def _default_skill_root() -> Path | None:
    home = os.environ.get("CODEX_HOME", "")
    if home:
        return coding_standards_skill_root(home)
    runner_temp = os.environ.get("RUNNER_TEMP", "")
    if not runner_temp:
        return None
    return coding_standards_skill_root(Path(runner_temp) / "codex-home")


def load_coding_standard_rule_ids(skill_root: str | Path | None = None) -> frozenset[str]:
    """Return exact rule ids from the installed finn-coding-standards index."""
    root = Path(skill_root) if skill_root is not None else _default_skill_root()
    if root is None or not root.is_dir():
        return frozenset()
    rule_ids: set[str] = set()
    for path in root.rglob("*.md"):
        if path.is_file():
            rule_ids.update(_RULE_BULLET.findall(path.read_text(encoding="utf-8")))
    return frozenset(rule_ids)


def _finding_text(value: object, *, max_length: int = MAX_FINDING_TEXT_LENGTH) -> str | None:
    if not isinstance(value, str):
        return None
    # Single-line fields keep the rendered `- **rule**` section headers unforgeable.
    text = " ".join(value.split())
    if not text or _contains_unsafe_markup(text):
        return None
    if len(text) > max_length:
        return None
    return text


def _downgraded_finding(index: int, reason: str, finding: object) -> dict[str, object]:
    return {FIELD_INDEX: index, FIELD_REASON: reason, FIELD_FINDING: finding}


def _complete_finding_fields(item: dict[str, object]) -> tuple[dict[str, str], list[str]]:
    normalized: dict[str, str] = {}
    missing_fields: list[str] = []
    for field in _FINDING_TEXT_FIELDS:
        value = _finding_text(item.get(field))
        if value is None:
            missing_fields.append(field)
            continue
        normalized[field] = value
    return normalized, missing_fields


class FilteredFindings(NamedTuple):
    kept: list[dict[str, str]]
    downgraded: list[dict[str, object]]


def _rejected(index: int, problem: str) -> RuntimeError:
    # A blocking finding that cannot be published must fail the review, never
    # vanish into an APPROVE.
    return RuntimeError(f"{FINDING_REJECTED_ERROR}: finding {index} {problem}")


def _classify_finding(
    item: object,
    index: int,
    known_ids: frozenset[str],
) -> tuple[dict[str, str], dict[str, object] | None]:
    """Return (published finding, downgrade record) for one raw finding.

    A finding whose rule id is not installed keeps its substance but is published
    under NO_COVERING_RULE, so the model cannot mint authority while a defect nobody
    wrote a rule for still surfaces. Malformed findings fail closed.
    """
    if not isinstance(item, dict):
        raise _rejected(index, "must be an object")
    rule = _finding_text(item.get(FIELD_RULE), max_length=200)
    if rule is None:
        raise _rejected(index, "is missing a rule id")
    normalized, missing_fields = _complete_finding_fields(item)
    if missing_fields:
        raise _rejected(index, "has an empty, oversized, or unsafe " + ", ".join(missing_fields))
    if rule == NO_COVERING_RULE or rule in known_ids:
        return {FIELD_RULE: rule, **normalized}, None
    downgrade = _downgraded_finding(
        index, f"{rule} {UNKNOWN_RULE_REASON_SUFFIX}; published as {NO_COVERING_RULE}", item
    )
    return {FIELD_RULE: NO_COVERING_RULE, **normalized}, downgrade


def filter_review_findings(
    raw_findings: object,
    *,
    known_rule_ids: frozenset[str] | None = None,
) -> FilteredFindings:
    """Publish complete findings, downgrading uninstalled rule ids; fail on malformed ones."""
    if raw_findings is None:
        return FilteredFindings([], [])
    if not isinstance(raw_findings, list):
        raise RuntimeError("findings must be an array")

    known_ids = known_rule_ids if known_rule_ids is not None else load_coding_standard_rule_ids()
    if raw_findings and not known_ids:
        raise RuntimeError(RULE_INDEX_UNAVAILABLE_ERROR)
    filtered = FilteredFindings([], [])
    for index, item in enumerate(raw_findings):
        complete, downgrade = _classify_finding(item, index, known_ids)
        if downgrade is not None:
            filtered.downgraded.append(downgrade)
        filtered.kept.append(complete)
    return filtered


def _omission_line(count: int, noun: str) -> str:
    plural = "" if count == 1 else "s"
    return f"- {count} more {noun}{plural} omitted from the published reason; see the filtered review artifact."


def _bounded_sections(sections: list[str], *, noun: str, limit: int) -> list[str]:
    """Keep leading sections while the joined text, plus any omission note, fits the limit."""
    rendered: list[str] = []
    for index, section in enumerate(sections):
        omitted_after = len(sections) - index - 1
        candidate = [*rendered, section]
        if omitted_after:
            candidate.append(_omission_line(omitted_after, noun))
        if len("\n".join(candidate)) > limit:
            break
        rendered.append(section)
    omitted = len(sections) - len(rendered)
    if omitted:
        rendered.append(_omission_line(omitted, noun))
    return rendered


def _finding_section(finding: dict[str, str]) -> str:
    lines = [f"- **{finding[FIELD_RULE]}**"]
    for field in _FINDING_TEXT_FIELDS:
        lines.append(f"  - {_FINDING_REASON_LABELS[field]}: {finding[field]}")
    return "\n".join(lines)


def observation_notes(raw_observations: object) -> list[str]:
    """Return sanitized observation notes; malformed entries are skipped."""
    if raw_observations is None:
        return []
    if not isinstance(raw_observations, list):
        raise RuntimeError("observations must be an array")
    notes: list[str] = []
    for item in raw_observations:
        note = _finding_text(item.get("note")) if isinstance(item, dict) else None
        if note is not None:
            notes.append(note)
    return notes


def render_verdict_reason(
    verdict: str,
    findings: list[dict[str, str]],
    *,
    observations: list[str] | None = None,
) -> str:
    """Publish kept findings, or the verdict plus observations, within the transport bound.

    Observations are published only for a non-approving verdict: the decision policy
    keeps them local on an APPROVE, where they would read as unrequested change requests.
    """
    if findings:
        sections = [_finding_section(finding) for finding in findings]
        return "\n".join(_bounded_sections(sections, noun="finding", limit=MAX_OUTPUT_TEXT_LENGTH))
    if verdict == VERDICT_APPROVE:
        return "No blocking findings."
    headline = f"Review verdict is {verdict}."
    notes = [f"- {note}" for note in observations or []]
    remaining = MAX_OUTPUT_TEXT_LENGTH - len(headline) - 1
    return "\n".join([headline, *_bounded_sections(notes, noun="observation", limit=remaining)])
