#!/usr/bin/env python3
"""Shared GitHub comment helpers for code-review authentication alerts."""

from __future__ import annotations

import json
from typing import Any

from scripts.code_review_common import GhOutput, gh_output


def gh_paginated_items(path: str, *, run_gh: GhOutput = gh_output) -> list[dict[str, Any]]:
    pages = json.loads(run_gh(["api", "--paginate", "--slurp", path]))
    items: list[dict[str, Any]] = []
    for page in pages:
        if isinstance(page, list):
            items.extend(page)
        else:
            items.append(page)
    return items


def latest_marked_issue_comment(
    comments: list[dict[str, Any]],
    *,
    bot_login: str,
    marker: str,
) -> dict[str, Any] | None:
    matches = [
        comment
        for comment in comments
        if comment.get("user", {}).get("login") == bot_login and f"<!-- {marker} -->" in (comment.get("body") or "")
    ]
    if not matches:
        return None
    return max(matches, key=lambda comment: int(comment["id"]))
