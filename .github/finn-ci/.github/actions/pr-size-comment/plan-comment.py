#!/usr/bin/env python3
"""Plan deterministic PR-size marker-comment reconciliation."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

MARKER = "<!-- reviewable-pr-size -->"
BOT_LOGIN = "github-actions[bot]"
GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def flatten_comments(payload: Any) -> list[dict[str, Any]]:
    """Accept one comments page or pages emitted by `gh --slurp`."""
    if not isinstance(payload, list):
        raise ValueError("GitHub comments response must be an array")

    comments: list[dict[str, Any]] = []
    for entry in payload:
        if isinstance(entry, list):
            comments.extend(comment for comment in entry if isinstance(comment, dict))
        elif isinstance(entry, dict):
            comments.append(entry)
    return comments


def marker_comment_ids(comments: list[dict[str, Any]]) -> list[int]:
    """Return bot-owned marker comment ids in deterministic order."""
    comment_ids = [
        comment["id"]
        for comment in comments
        if isinstance(comment.get("id"), int)
        and not isinstance(comment.get("id"), bool)
        and isinstance(comment.get("user"), dict)
        and comment["user"].get("login") == BOT_LOGIN
        and isinstance(comment.get("body"), str)
        and MARKER in comment["body"]
    ]
    return sorted(set(comment_ids))


def comment_body(*, mode: str, reviewable_loc: int, reviewable_files: int, blocked: bool, override_actor: str) -> str:
    """Render the stable marker-comment body."""
    overridden = bool(override_actor)
    state = "override accepted" if overridden else "blocked" if blocked else "warning"
    if overridden:
        guidance = f"@{override_actor} approved this PR-size exception."
    elif blocked:
        approver = "Jai" if reviewable_loc > 2000 or reviewable_files > 80 else "an authorized approver"
        guidance = f"Split the PR, or ask {approver} to comment `/pr-size-override`."
    else:
        guidance = "Consider splitting the PR before requesting review."

    return "\n".join(
        [
            MARKER,
            "## PR size",
            "",
            f"Reviewable change: **{reviewable_loc} LOC** across **{reviewable_files} files**.",
            f"Mode: **{mode}**. Result: **{state}**.",
            "",
            guidance,
        ]
    )


def plan(
    comments: list[dict[str, Any]],
    *,
    mode: str,
    reviewable_loc: int,
    reviewable_files: int,
    warned: bool,
    blocked: bool,
    override_actor: str,
) -> dict[str, list[dict[str, Any]]]:
    """Return ordered mutations that restore the single-comment invariant."""
    comment_ids = marker_comment_ids(comments)
    if not warned:
        return {"operations": [{"operation": "delete", "comment_id": comment_id} for comment_id in comment_ids]}

    body = comment_body(
        mode=mode,
        reviewable_loc=reviewable_loc,
        reviewable_files=reviewable_files,
        blocked=blocked,
        override_actor=override_actor,
    )
    if not comment_ids:
        return {"operations": [{"operation": "create", "body": body}]}

    canonical_comment_id, *duplicate_comment_ids = comment_ids
    return {
        "operations": [
            {
                "operation": "update",
                "comment_id": canonical_comment_id,
                "body": body,
            },
            *[{"operation": "delete", "comment_id": comment_id} for comment_id in duplicate_comment_ids],
        ]
    }


def parse_bool(value: str) -> bool:
    """Parse one exact lowercase Boolean input."""
    if value not in {"true", "false"}:
        raise argparse.ArgumentTypeError("must be 'true' or 'false'")
    return value == "true"


def non_negative_integer(value: str) -> int:
    """Parse one non-negative integer input."""
    if not value.isdigit():
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return int(value)


def github_login(value: str) -> str:
    """Accept an empty value or one GitHub login."""
    if value and not GITHUB_LOGIN.fullmatch(value):
        raise argparse.ArgumentTypeError("must be empty or a GitHub login")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("warn", "enforce"), required=True)
    parser.add_argument("--reviewable-loc", type=non_negative_integer, required=True)
    parser.add_argument("--reviewable-files", type=non_negative_integer, required=True)
    parser.add_argument("--warned", type=parse_bool, required=True)
    parser.add_argument("--blocked", type=parse_bool, required=True)
    parser.add_argument("--override-actor", type=github_login, required=True)
    arguments = parser.parse_args()

    try:
        comments = flatten_comments(json.load(sys.stdin))
    except (ValueError, json.JSONDecodeError) as error:
        print(f"Could not plan PR-size comment: {error}", file=sys.stderr)
        return 1

    if arguments.blocked and not arguments.warned:
        print("Could not plan PR-size comment: blocked requires warned", file=sys.stderr)
        return 1

    json.dump(
        plan(
            comments,
            mode=arguments.mode,
            reviewable_loc=arguments.reviewable_loc,
            reviewable_files=arguments.reviewable_files,
            warned=arguments.warned,
            blocked=arguments.blocked,
            override_actor=arguments.override_actor,
        ),
        sys.stdout,
        separators=(",", ":"),
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
