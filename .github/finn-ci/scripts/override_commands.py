"""Parse AI-review, GrowthBook, and PR-size override comments."""

from __future__ import annotations

from dataclasses import dataclass

AI_REVIEW_OVERRIDE_COMMAND = "/ai-review-override"
GROWTHBOOK_OVERRIDE_COMMAND = "/growthbook-override"
GROWTHBOOK_OVERRIDE_ALIAS = "/no-experiment"
PR_SIZE_OVERRIDE_COMMAND = "/pr-size-override"


@dataclass(frozen=True)
class OverrideCommand:
    """Identify one canonical override target."""

    name: str


REGISTERED_OVERRIDE_COMMAND_TARGETS: tuple[tuple[str, str], ...] = (
    (AI_REVIEW_OVERRIDE_COMMAND, AI_REVIEW_OVERRIDE_COMMAND),
    (GROWTHBOOK_OVERRIDE_COMMAND, GROWTHBOOK_OVERRIDE_COMMAND),
    (GROWTHBOOK_OVERRIDE_ALIAS, GROWTHBOOK_OVERRIDE_COMMAND),
    (PR_SIZE_OVERRIDE_COMMAND, PR_SIZE_OVERRIDE_COMMAND),
)


def parse_override_command(
    body: str,
    *,
    canonical: str,
) -> OverrideCommand | None:
    """Parse one canonical override command.

    The complete comment body must equal `canonical`.

    Args:
        body: Complete GitHub comment body.
        canonical: Canonical slash-command name.

    Returns:
        The canonical command, or None if the body does not match.
    """
    if not isinstance(body, str):
        return None

    if body == canonical:
        return OverrideCommand(name=canonical)

    return None


def parse_registered_override_command(
    body: str,
    *,
    canonical: str | None = None,
) -> OverrideCommand | None:
    """Return the first registered override that matches the comment body."""
    for command in parse_registered_override_commands(body):
        if not canonical or command.name == canonical:
            return command
    return None


def parse_registered_override_commands(body: str) -> list[OverrideCommand]:
    """Return all canonical overrides that match the comment body."""
    if not isinstance(body, str):
        return []

    commands: list[OverrideCommand] = []
    for registered, target in REGISTERED_OVERRIDE_COMMAND_TARGETS:
        if parse_override_command(body, canonical=registered):
            commands.append(OverrideCommand(name=target))
    return commands
