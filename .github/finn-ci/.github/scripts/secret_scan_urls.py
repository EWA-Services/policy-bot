"""Shared HTTPS and GitHub URL policy for secret-scan notifications."""

from __future__ import annotations

import re
from urllib.parse import urlparse


def require_https_url(url: str, label: str) -> str:
    """Require HTTPS with a valid host, port, and no embedded credentials."""
    parsed_url = urlparse(url)
    if parsed_url.scheme.lower() != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password:
        raise ValueError(f"{label} must use HTTPS without credentials")
    try:
        parsed_url.port
    except ValueError as error:
        raise ValueError(f"{label} has an invalid port") from error
    return url


def https_origin(url: str, label: str) -> tuple[str, str, int]:
    """Return a normalized HTTPS origin."""
    parsed_url = urlparse(require_https_url(url, label))
    return "https", parsed_url.hostname.lower(), parsed_url.port or 443


def require_same_https_origin(url: str, trusted_origin: str, label: str) -> str:
    """Require a URL to stay on a trusted HTTPS origin."""
    if https_origin(url, label) != https_origin(trusted_origin, "Trusted origin"):
        raise ValueError(f"{label} must use the trusted HTTPS origin")
    return url


def require_pull_request_number(value: str) -> str:
    """Require a canonical positive GitHub pull request number."""
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("GitHub pull request number must be a positive integer")
    return value


def require_github_web_url(
    url: str,
    github_server_url: str,
    repository: str,
    route_pattern: str,
    label: str,
) -> str:
    """Validate a display URL against the trusted GitHub web origin and route."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GitHub repository has an invalid name")
    parsed_server = urlparse(github_server_url)
    parsed_url = urlparse(url)
    if https_origin(url, label) != https_origin(github_server_url, "GitHub server URL"):
        raise ValueError(f"{label} must use the GitHub server origin")
    if parsed_server.path not in ("", "/") or parsed_server.query or parsed_server.fragment:
        raise ValueError("GitHub server URL must contain only an origin")
    expected_path = rf"/{re.escape(repository)}/{route_pattern}"
    if not re.fullmatch(expected_path, parsed_url.path) or parsed_url.params or parsed_url.query or parsed_url.fragment:
        raise ValueError(f"{label} has an unexpected GitHub path")
    return url
