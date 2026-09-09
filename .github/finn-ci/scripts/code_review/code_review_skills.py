#!/usr/bin/env python3
"""Validate and resolve the canonical skills used by code review."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def validate_catalog_token(env: dict[str, str]) -> None:
    if not env.get("AI_REVIEW_SKILL_CATALOG_TOKEN"):
        raise ValueError("Need FINN_DEVOPS_PERSONAL_ACCESS_TOKEN to read EWA-Services/agent-resources.")


def install_review_skill(catalog_root: Path, codex_home: Path) -> Path:
    source = catalog_root / "skills/finn-pr-review"
    skill_file = source / "SKILL.md"
    if not skill_file.is_file():
        raise FileNotFoundError(
            f"Missing the required review process skill at {skill_file} in EWA-Services/agent-resources."
        )

    destination = codex_home / "skills/finn-pr-review"
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    print(f"Installed finn-pr-review at {destination}.")
    return destination


def install_coding_standards_skill(catalog_root: Path, codex_home: Path) -> Path:
    """Install finn-coding-standards; the findings contract cannot validate rule ids without it."""
    source = catalog_root / "skills/finn-coding-standards"
    skill_file = source / "SKILL.md"
    if not skill_file.is_file():
        raise FileNotFoundError(
            f"Missing the required coding-standards skill at {skill_file} in EWA-Services/agent-resources."
        )

    destination = codex_home / "skills/finn-coding-standards"
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    print(f"Installed finn-coding-standards at {destination}.")
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate-catalog-token", "install"))
    parser.add_argument("--catalog-root", default=".agent-resources")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-catalog-token":
            validate_catalog_token(dict(os.environ))
        else:
            if not args.codex_home:
                raise ValueError("CODEX_HOME or --codex-home is required")
            catalog_root = Path(args.catalog_root)
            codex_home = Path(args.codex_home)
            install_review_skill(catalog_root, codex_home)
            install_coding_standards_skill(catalog_root, codex_home)
    except (KeyError, OSError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
