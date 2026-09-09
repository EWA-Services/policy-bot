#!/usr/bin/env python3
"""Install the pinned command-line tools used by code review."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def append_github_path(env: dict[str, str], path: Path) -> None:
    if not env.get("GITHUB_PATH"):
        return
    with Path(env["GITHUB_PATH"]).open("a", encoding="utf-8") as output:
        output.write(f"{path}\n")


def install_codex(
    env: dict[str, str],
    *,
    run_command: RunCommand = subprocess.run,
) -> None:
    prefix = Path(env["RUNNER_TEMP"]) / "npm-global"
    run_command(["mkdir", "-p", str(prefix)], check=True, text=True)
    run_command(["npm", "config", "set", "prefix", str(prefix)], check=True, text=True)

    command_env = dict(env)
    command_env["PATH"] = f"{prefix}/bin:{env.get('PATH', '')}"
    append_github_path(env, prefix / "bin")
    version = env.get("CODEX_VERSION") or "latest"
    run_command(
        ["npm", "install", "-g", "--ignore-scripts", f"@openai/codex@{version}"],
        check=True,
        text=True,
        env=command_env,
    )
    run_command(["codex", "--version"], check=True, text=True, env=command_env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install-codex",))
    parser.parse_args(argv)
    try:
        install_codex(dict(os.environ))
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
