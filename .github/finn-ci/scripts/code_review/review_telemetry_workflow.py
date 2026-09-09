"""Transport count-only review evidence between jobs; write only after publication."""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from build_review_telemetry import build_review_event
from write_review_telemetry import write_event


def stage_telemetry(stage: str) -> dict:
    if stage == "review":
        review = json.loads(Path("codex-review-filtered.json").read_text())
        return {
            "verdict": review["verdict"],
            "findings": [{"rule": finding["rule"]} for finding in review["findings"]],
            "downgraded_findings": [{"index": finding["index"]} for finding in review.get("downgraded_findings", [])],
        }
    admission = json.loads(Path("codex-admission.json").read_text())
    judge = json.loads(Path("codex-admission-output.txt").read_text())
    return {
        "admission": {name: admission[name] for name in ("classification", "admittedFindings", "protectedFindings")},
        "judge": {"admittedFindings": judge["admittedFindings"]},
    }


def publish_telemetry(env: dict[str, str]) -> None:
    review = json.loads(base64.b64decode(env["TELEMETRY_REVIEW"], validate=True))
    admission = (
        json.loads(base64.b64decode(env["TELEMETRY_ADMISSION"], validate=True))
        if env.get("TELEMETRY_ADMISSION")
        else {}
    )
    # A finish-job retry still describes the original model invocation.
    run_id, attempt = map(int, env["REVIEW_INVOCATION_ID"].split(":"))
    context = {
        "repository": env["GITHUB_REPOSITORY"],
        "pull_request": int(env["TARGET_PR_NUMBER"]),
        "head_sha": env["REVIEW_HEAD_SHA"],
        "run_id": run_id,
        "run_attempt": attempt,
        "final_verdict": env["FINAL_VERDICT"],
    }
    event = build_review_event(context, review, admission.get("admission"), admission.get("judge"))
    write_event(event, env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("review", "admission", "publish"))
    stage = parser.parse_args().stage
    try:
        if stage == "publish":
            publish_telemetry(dict(os.environ))
            print("Review telemetry event accepted.")
        else:
            encoded = base64.b64encode(json.dumps(stage_telemetry(stage), allow_nan=False).encode()).decode()
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write(f"telemetry={encoded}\n")
    except (ValueError, TypeError, KeyError, OSError, RuntimeError):
        print("::warning::Review telemetry failed. Check artifacts and Supabase configuration.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
