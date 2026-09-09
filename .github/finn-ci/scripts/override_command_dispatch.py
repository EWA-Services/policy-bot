#!/usr/bin/env python3
"""Route override comment lifecycle events to consumer workflows."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.override_commands import parse_registered_override_commands


def command_names(body: str) -> tuple[str, ...]:
    """Return registered command names for one comment."""
    return tuple(command.name.removeprefix("/") for command in parse_registered_override_commands(body))


def dispatch_commands_for_event(event: dict[str, Any]) -> tuple[str, ...]:
    """Return override workflows that require dispatch for this comment event."""
    action = event.get("action")
    issue = event.get("issue")
    comment = event.get("comment")
    if action not in {"created", "edited", "deleted"}:
        return ()
    if not isinstance(issue, dict) or "pull_request" not in issue or issue.get("state") != "open":
        return ()
    if not isinstance(comment, dict):
        return ()

    user = comment.get("user")
    author = user.get("login") if isinstance(user, dict) else ""
    if not isinstance(author, str) or author.endswith("[bot]"):
        return ()

    body = comment.get("body")
    current = command_names(body if isinstance(body, str) else "")
    previous_body = event.get("changes", {}).get("body", {}).get("from", "")
    previous = command_names(previous_body if isinstance(previous_body, str) else "")

    if action == "created":
        return ()
    if action == "deleted":
        return tuple(sorted(current))

    commands = {*current, *previous}
    return tuple(sorted(commands))


def dispatch_payload(event: dict[str, Any], command: str) -> dict[str, Any]:
    """Build the bounded comment context forwarded to one override workflow.

    Current and previous comment bodies are limited to 4,096 characters.
    """
    issue = event["issue"]
    comment = event["comment"]
    source_payload = {
        "action": event["action"],
        "issue": {
            "number": issue["number"],
            "state": issue["state"],
            "pull_request": issue["pull_request"],
        },
        "comment": {
            "id": comment["id"],
            "body": str(comment.get("body", ""))[:4096],
            "user": {"login": comment["user"]["login"]},
        },
    }
    if event.get("changes"):
        previous_body = event["changes"].get("body", {}).get("from", "")
        source_payload["changes"] = {"body": {"from": str(previous_body)[:4096]}}
    return {
        "event_type": f"{command}-command",
        "client_payload": {
            "slash_command": {"command": command},
            "github": {"payload": source_payload},
        },
    }


def dispatch_event(*, repository: str, event: dict[str, Any]) -> int:
    """Send one repository-dispatch event for each selected override workflow."""
    commands = dispatch_commands_for_event(event)
    for command in commands:
        subprocess.run(
            ["gh", "api", "--method", "POST", f"repos/{repository}/dispatches", "--input", "-"],
            input=json.dumps(dispatch_payload(event, command)),
            check=True,
            text=True,
        )
        print(f"Dispatched {command}-command.")
    if not commands:
        print("No override lifecycle command to dispatch.")
    return 0


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not event_path or not repository:
        print("GITHUB_EVENT_PATH and GITHUB_REPOSITORY are required.", file=sys.stderr)
        return 1
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        return dispatch_event(repository=repository, event=event)
    except (json.JSONDecodeError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"Could not dispatch override command event: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
