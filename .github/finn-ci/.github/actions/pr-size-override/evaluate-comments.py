#!/usr/bin/env python3
"""Select a current authorized PR-size override without emitting its reason."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.override_commands import PR_SIZE_OVERRIDE_COMMAND, parse_registered_override_command  # noqa: E402

STANDARD_MAX_LOC = 2000
STANDARD_MAX_FILES = 80


def flatten_comments(payload: Any) -> list[dict[str, Any]]:
    """Accept one comments page or the page array emitted by `gh --slurp`."""
    if not isinstance(payload, list):
        raise ValueError("GitHub comments response must be an array")

    comments: list[dict[str, Any]] = []
    for entry in payload:
        if isinstance(entry, list):
            comments.extend(comment for comment in entry if isinstance(comment, dict))
        elif isinstance(entry, dict):
            comments.append(entry)
    return comments


def override_tier(
    comment: dict[str, Any],
    *,
    approved_users: set[str],
    reviewable_loc: int,
    reviewable_files: int,
) -> str:
    """Return the effective authorization tier for one current comment."""
    user = comment.get("user")
    body = comment.get("body")
    comment_url = comment.get("html_url")
    comment_id = comment.get("id")
    if not (
        isinstance(user, dict)
        and isinstance(body, str)
        and parse_registered_override_command(
            body,
            canonical=PR_SIZE_OVERRIDE_COMMAND,
        )
        and isinstance(comment_url, str)
        and bool(comment_url)
        and isinstance(comment_id, int)
        and not isinstance(comment_id, bool)
    ):
        return ""

    actor = user.get("login")
    if actor == "jai":
        return "jai"
    if actor in approved_users and reviewable_loc <= STANDARD_MAX_LOC and reviewable_files <= STANDARD_MAX_FILES:
        return "standard"
    return ""


def select_override(
    comments: list[dict[str, Any]],
    *,
    approved_users: set[str],
    reviewable_loc: int,
    reviewable_files: int,
) -> tuple[dict[str, Any], str] | None:
    """Select the earliest valid current comment by immutable GitHub id."""
    valid_comments = [
        (comment, tier)
        for comment in comments
        if (
            tier := override_tier(
                comment,
                approved_users=approved_users,
                reviewable_loc=reviewable_loc,
                reviewable_files=reviewable_files,
            )
        )
    ]
    return min(valid_comments, key=lambda item: item[0]["id"], default=None)


def append_outputs(output_path: Path, selection: tuple[dict[str, Any], str] | None) -> None:
    """Write only non-sensitive override metadata to the action output file."""
    comment, tier = selection if selection else ({}, "")
    actor = comment.get("user", {}).get("login", "")
    comment_url = comment.get("html_url", "")
    with output_path.open("a", encoding="utf-8") as output_file:
        output_file.write(f"actor={actor}\n")
        output_file.write(f"tier={tier}\n")
        output_file.write(f"comment_url={comment_url}\n")


def read_tier_configuration() -> tuple[set[str], int, int]:
    """Read required standard-tier inputs from the workflow environment.

    The inputs are `PR_SIZE_OVERRIDE_APPROVERS`, `PR_SIZE_REVIEWABLE_LOC`, and
    `PR_SIZE_REVIEWABLE_FILES`.

    Returns:
        The approver logins, reviewable LOC, and reviewable file count.

    Raises:
        ValueError: If an input is missing or a measurement is invalid.
    """
    approved_users_value = os.environ.get("PR_SIZE_OVERRIDE_APPROVERS", "").strip()
    reviewable_loc_value = os.environ.get("PR_SIZE_REVIEWABLE_LOC", "").strip()
    reviewable_files_value = os.environ.get("PR_SIZE_REVIEWABLE_FILES", "").strip()
    if not approved_users_value or not reviewable_loc_value or not reviewable_files_value:
        raise ValueError("tier inputs are required")

    reviewable_loc = int(reviewable_loc_value)
    reviewable_files = int(reviewable_files_value)
    if reviewable_loc < 0 or reviewable_files < 0:
        raise ValueError("reviewable measurements must be non-negative integers")
    return set(approved_users_value.split()), reviewable_loc, reviewable_files


def main() -> int:
    output_path_value = os.environ.get("GITHUB_OUTPUT")
    if not output_path_value:
        print("GITHUB_OUTPUT is required", file=sys.stderr)
        return 1

    try:
        approved_users, reviewable_loc, reviewable_files = read_tier_configuration()
        payload = json.load(sys.stdin)
        comments = flatten_comments(payload)
        append_outputs(
            Path(output_path_value),
            select_override(
                comments,
                approved_users=approved_users,
                reviewable_loc=reviewable_loc,
                reviewable_files=reviewable_files,
            ),
        )
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Could not evaluate PR-size override comments: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
