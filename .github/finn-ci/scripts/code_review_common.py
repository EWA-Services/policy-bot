#!/usr/bin/env python3
"""Shared utilities for the direct CLI code-review workflow."""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Callable
from typing import Any

GhOutput = Callable[[list[str]], str]


def output_values(values: dict[str, Any]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            text = "" if value is None else str(value)
            if "\n" in text:
                delimiter = f"EOF_{key}_{uuid.uuid4().hex}"
                while f"\n{delimiter}\n" in f"\n{text}\n":
                    delimiter = f"EOF_{key}_{uuid.uuid4().hex}"
                fh.write(f"{key}<<{delimiter}\n{text}\n{delimiter}\n")
            else:
                fh.write(f"{key}={text}\n")


def gh_output(args: list[str]) -> str:
    return subprocess.check_output(["gh", *args], text=True, stderr=subprocess.DEVNULL)
