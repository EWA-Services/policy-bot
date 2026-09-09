#!/usr/bin/env python3
"""Evaluate PR-size thresholds and emit structured telemetry."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

WARN_LOC = 400
WARN_FILES = 15
BLOCK_LOC = 1000
BLOCK_FILES = 40
STANDARD_OVERRIDE_MAX_LOC = 2000
STANDARD_OVERRIDE_MAX_FILES = 80
NON_NEGATIVE_INTEGER = re.compile(r"^[0-9]+$")
POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]*$")


def parse_non_negative_integer(value: str) -> int | None:
    """Return a non-negative integer, or None for invalid input."""
    if not NON_NEGATIVE_INTEGER.fullmatch(value):
        return None
    return int(value)


def append_output(output_path: Path, name: str, value: str) -> None:
    """Append one single-line GitHub Actions output."""
    with output_path.open("a", encoding="utf-8") as output_file:
        output_file.write(f"{name}={value}\n")


def main() -> int:
    mode = os.environ.get("PR_SIZE_MODE", "warn")
    raw_loc = parse_non_negative_integer(os.environ.get("PR_SIZE_RAW_LOC", ""))
    reviewable_loc = parse_non_negative_integer(os.environ.get("PR_SIZE_REVIEWABLE_LOC", ""))
    reviewable_files = parse_non_negative_integer(os.environ.get("PR_SIZE_REVIEWABLE_FILES", ""))
    repository = os.environ.get("PR_SIZE_REPOSITORY", "")
    pull_request_value = os.environ.get("PR_SIZE_PULL_REQUEST_NUMBER", "")
    head_sha = os.environ.get("PR_SIZE_HEAD_SHA", "")
    override_actor_value = os.environ.get("PR_SIZE_OVERRIDE_ACTOR", "")
    override_tier_value = os.environ.get("PR_SIZE_OVERRIDE_TIER", "")

    pull_request = int(pull_request_value) if POSITIVE_INTEGER.fullmatch(pull_request_value) else None
    configuration_error = (
        mode not in {"warn", "enforce"}
        or raw_loc is None
        or reviewable_loc is None
        or reviewable_files is None
        or not repository
        or pull_request is None
        or not head_sha
    )

    threshold_result = "configuration-error"
    if not configuration_error:
        if reviewable_loc > BLOCK_LOC or reviewable_files > BLOCK_FILES:
            threshold_result = "blocking-threshold-exceeded"
        elif reviewable_loc > WARN_LOC or reviewable_files > WARN_FILES:
            threshold_result = "warning-threshold-exceeded"
        else:
            threshold_result = "under-limit"

    # Revalidate standard-tier limits at the final blocking decision.
    override_is_valid = (override_actor_value == "jai" and override_tier_value == "jai") or (
        bool(override_actor_value)
        and override_actor_value != "jai"
        and override_tier_value == "standard"
        and reviewable_loc is not None
        and reviewable_loc <= STANDARD_OVERRIDE_MAX_LOC
        and reviewable_files is not None
        and reviewable_files <= STANDARD_OVERRIDE_MAX_FILES
    )
    override_actor = override_actor_value if override_is_valid else None
    override_tier = None
    override_comment_url = None
    if override_actor:
        override_tier = override_tier_value
        override_comment_url = os.environ.get("PR_SIZE_OVERRIDE_COMMENT_URL") or None
    warned = threshold_result in {
        "warning-threshold-exceeded",
        "blocking-threshold-exceeded",
    }
    blocked = mode == "enforce" and threshold_result == "blocking-threshold-exceeded" and override_actor is None

    telemetry = json.dumps(
        {
            "schema": "finn-pr-size/v1",
            "repository": repository,
            "pull_request": pull_request,
            "head_sha": head_sha,
            "mode": mode,
            "raw_loc": raw_loc,
            "reviewable_loc": reviewable_loc,
            "reviewable_files": reviewable_files,
            "threshold_result": threshold_result,
            "override_actor": override_actor,
            "override_tier": override_tier,
            "override_comment_url": override_comment_url,
        },
        separators=(",", ":"),
    )

    output_path = Path(os.environ["GITHUB_OUTPUT"])
    append_output(output_path, "blocked", str(blocked).lower())
    append_output(output_path, "configuration-error", str(configuration_error).lower())
    append_output(output_path, "threshold-result", threshold_result)
    append_output(output_path, "warned", str(warned).lower())
    append_output(output_path, "telemetry", telemetry)

    summary_path = Path(os.environ["GITHUB_STEP_SUMMARY"])
    with summary_path.open("a", encoding="utf-8") as summary_file:
        summary_file.write(f"## PR size telemetry\n\n`{telemetry}`\n")

    print(telemetry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
