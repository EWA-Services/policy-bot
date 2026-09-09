"""Build count-only telemetry from filtered review and admission JSON artifacts.

Pipe stdout to write_review_telemetry.py. The context JSON supplies repository,
pull_request, head_sha, run_id, run_attempt and the optional published final_verdict.
--judge accepts the raw judge output; --admission accepts the resolved admission.
Unknown decisions stay null. Finding prose is never copied into the event.
"""

import argparse
import json
import re
import sys
from pathlib import Path


def _finding_indices(indices: object, count: int) -> set[int]:
    if not isinstance(indices, list) or any(type(index) is not int or not 0 <= index < count for index in indices):
        raise ValueError("Invalid finding indices")
    return set(indices)


def build_review_event(context: dict, review: dict, admission: dict | None = None, judge: dict | None = None) -> dict:
    """Summarize the combined structured-findings contract without judging findings."""
    repository = context["repository"]
    if not isinstance(repository, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("Invalid repository")
    for name in ("pull_request", "run_id", "run_attempt"):
        if type(context[name]) is not int or context[name] < 1:
            raise ValueError("Invalid GitHub identifier")
    if not isinstance(context["head_sha"], str) or not re.fullmatch(r"[a-fA-F0-9]{40}", context["head_sha"]):
        raise ValueError("Invalid head SHA")
    if review["verdict"] not in {"APPROVE", "REQUEST_CHANGES", "WAIT"}:
        raise ValueError("Invalid primary verdict")
    final_verdict = context.get("final_verdict")
    if final_verdict not in {None, "approved", "requested_changes", "none", "error", "skipped"}:
        raise ValueError("Invalid final verdict")
    findings = review["findings"]
    if not isinstance(findings, list) or any(
        not isinstance(finding, dict)
        or not isinstance(finding.get("rule"), str)
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", finding["rule"])
        for finding in findings
    ):
        raise ValueError("Invalid structured findings")
    all_indices = set(range(len(findings)))
    downgraded = _finding_indices([entry["index"] for entry in review.get("downgraded_findings", [])], len(findings))
    admitted = protected = judged = None
    if admission is not None:
        admitted = _finding_indices(admission["admittedFindings"], len(findings))
        protected = _finding_indices(admission["protectedFindings"], len(findings))
        if not protected <= admitted or admission["classification"] != ("ADMIT" if admitted else "REJECT"):
            raise ValueError("Inconsistent resolved admission")
    if judge is not None:
        judged = _finding_indices(judge["admittedFindings"], len(findings))
        if admitted is not None and admitted != judged | protected:
            raise ValueError("Judge and resolved admission do not match")
    groups = {
        "total": all_indices,
        "admitted": admitted,
        "rejected": None if admitted is None else all_indices - admitted,
        "protected": protected,
        "judge_admitted": judged,
        "judge_rejected": None if judged is None else all_indices - judged,
        "protection_overrides": None if judged is None or protected is None else protected - judged,
        "rule_downgraded": downgraded,
    }
    by_rule = {}
    for index, finding in enumerate(findings):
        counts = by_rule.setdefault(
            finding["rule"], {name: None if indices is None else 0 for name, indices in groups.items()}
        )
        for name, indices in groups.items():
            if indices is not None and index in indices:
                counts[name] += 1
    run_id, attempt = context["run_id"], context["run_attempt"]
    return {
        "event_id": f"{repository}:{run_id}:{attempt}:review.completed.v1",
        "event_type": "review.completed.v1",
        "payload": {
            "repository": repository,
            "pull_request": context["pull_request"],
            "head_sha": context["head_sha"],
            "run_id": run_id,
            "run_attempt": attempt,
            "pr_url": f"https://github.com/{repository}/pull/{context['pull_request']}",
            "run_url": f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}",
            "primary_verdict": review["verdict"],
            "final_verdict": final_verdict,
            "admission_classification": None if admission is None else admission["classification"],
            "counts": {name: None if indices is None else len(indices) for name, indices in groups.items()},
            "by_rule": by_rule,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--admission", type=Path)
    parser.add_argument("--judge", type=Path)
    args = parser.parse_args()
    try:
        artifacts = [
            json.loads(path.read_text(encoding="utf-8")) if path else None
            for path in (args.context, args.review, args.admission, args.judge)
        ]
        print(json.dumps(build_review_event(*artifacts), allow_nan=False))
    except (ValueError, TypeError, KeyError, OSError):
        print("Cannot build review telemetry: invalid or unavailable artifacts.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
