"""Write one JSON event from stdin to Supabase; use the same event_id on retry.

Environment: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (legacy service-role JWT).
Input: {"event_id": "unique-id", "event_type": "review.completed.v1", "payload": {}}.
Payloads should contain counts, decisions and GitHub links, never secrets or PII.
Apply review_telemetry_schema.sql to the chosen telemetry database before use.
"""

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirects(HTTPRedirectHandler):
    """Keep database credentials on the configured endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def write_event(event: dict, url: str, key: str) -> None:
    """Insert once by event_id. A duplicate keeps the original event unchanged."""
    if (
        not isinstance(event, dict)
        or set(event) != {"event_id", "event_type", "payload"}
        or not all(isinstance(event[name], str) and event[name].strip() for name in ("event_id", "event_type"))
        or not isinstance(event["payload"], dict)
    ):
        raise ValueError("Expected event_id, event_type and an object payload.")
    endpoint = urlsplit(url)
    local_http = endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not key.strip()
        or not endpoint.hostname
        or (endpoint.scheme != "https" and not local_http)
        or endpoint.username
        or endpoint.password
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("Set a Supabase project URL and service-role key; HTTPS is required except on localhost.")
    request = Request(
        f"{url.rstrip('/')}/rest/v1/ai_review_events?on_conflict=event_id",
        data=json.dumps(event, allow_nan=False).encode("utf-8"),
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=ignore-duplicates,return=minimal",
        },
        method="POST",
    )
    try:
        with build_opener(NoRedirects()).open(request, timeout=10):
            pass
    except HTTPError as error:
        error.close()
        raise RuntimeError(f"Telemetry write failed: HTTP {error.code}") from None
    except (URLError, OSError):
        raise RuntimeError("Telemetry write failed: connection error") from None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        write_event(event, os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""))
    except (ValueError, RuntimeError):
        # Invalid input and HTTP error bodies may include private data.
        print("Telemetry write failed. Check configuration, event format and endpoint availability.", file=sys.stderr)
        return 1
    print("Telemetry event accepted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
