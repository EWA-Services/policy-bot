from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

EWA_ACTIONS_ROOT = Path(__file__).resolve().parents[4]
EWA_ACTIONS_SCRIPTS = EWA_ACTIONS_ROOT / "scripts"
if str(EWA_ACTIONS_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(EWA_ACTIONS_SCRIPTS))

import finn_ai_coder_github_app as finn_ai_metadata  # noqa: E402

DEFAULT_PATTERNS = [
    r"<!--\s*direct-cli-review:codex\s*-->",
    r"<!--\s*finn-ai-coder-review-metadata",
    r"##\s*Code Review Summary",
    r"(?:^|\n)##\s*Codex Review\b",
]
DEFAULT_ACTOR_PATTERNS = [
    r"^finn-codex$",
    r"^finn-ai-coder\[bot\]$",
]
AI_REVIEW_PATTERNS = [
    ("codex", r"<!--\s*direct-cli-review:codex\s*-->"),
    ("finn-ai-coder", r"<!--\s*finn-ai-coder-review-metadata"),
    ("codex", r"(?:^|\n)##\s*Codex Review\b"),
]
ACTOR_REVIEW_MARKERS = [
    r"Review posted on PR",
    r"What I checked",
    r"Inline comments added",
    r"inline PR review comment",
    r"I posted this as an inline",
]
COMMENT_MARKER = "<!-- pr-metadata-gate -->"
AUTOMATION_AUTHORS = frozenset({"finn-devops", "dependabot[bot]"})


@dataclass(frozen=True)
class TriggerInputs:
    event: str
    action: str = ""
    pr_number: str = ""
    pr_author: str = ""
    issue_number: str = ""
    issue_pr_url: str = ""
    comment_body: str = ""
    comment_author: str = ""
    review_body: str = ""
    review_author: str = ""
    repo_full: str = ""
    repo_owner: str = ""
    workflow_actor: str = ""


@dataclass(frozen=True)
class TriggerDecision:
    should_run: bool
    pr_number: str
    reason: str


@dataclass(frozen=True)
class ReviewEntry:
    entry_type: str
    actor: str
    body: str
    created_at: str
    url: str


@dataclass(frozen=True)
class AiReviewMatch:
    entry_type: str
    actor: str
    source: str
    created_at: str
    url: str


@dataclass(frozen=True)
class AiReviewState:
    present: bool = False
    source: str = ""
    actor: str = ""
    url: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class EvaluationResult:
    overall_status: str
    comment_text: str


class GitHubApi:
    def __init__(self, token: str) -> None:
        self.token = token

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-metadata-gate",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.github.com/{path.lstrip('/')}",
            data=data,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
        if not raw:
            return None
        return json.loads(raw)

    def paginate(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        separator = "&" if "?" in path else "?"
        while True:
            page_items = self.request_json(
                "GET",
                f"{path}{separator}per_page=100&page={page}",
            )
            if not isinstance(page_items, list):
                raise ValueError(f"Expected paginated list from {path}")
            items.extend(page_items)
            if len(page_items) < 100:
                return items
            page += 1

    def safe_fetch_pr_details(self, repository: str, number: str) -> tuple[str, str]:
        if not repository or not number:
            return ("", "")
        try:
            data = self.request_json("GET", f"repos/{repository}/pulls/{number}")
        except (
            OSError,
            TimeoutError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ):
            return ("", "")
        repo_info = (data.get("head") or {}).get("repo") or {}
        owner_info = repo_info.get("owner") or {}
        head_owner = (owner_info.get("login") or "").strip()
        author_login = ((data.get("user") or {}).get("login") or "").strip()
        return (head_owner, author_login)


def load_pattern_sources(raw: str | None, default: list[str]) -> list[str]:
    if not raw:
        return list(default)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return list(default)
    if not isinstance(parsed, list) or not parsed:
        return list(default)
    return [str(item) for item in parsed]


def matches_any(pattern_sources: list[str], text: str) -> bool:
    return any(re.search(pattern, text or "", re.IGNORECASE) for pattern in pattern_sources)


def matches_actor_review(
    actor: str,
    text: str,
    actor_pattern_sources: list[str],
    actor_review_markers: list[str] | None = None,
) -> bool:
    actor_review_markers = actor_review_markers or ACTOR_REVIEW_MARKERS
    if not matches_any(actor_pattern_sources, actor):
        return False
    return any(re.search(marker, text or "", re.IGNORECASE) for marker in actor_review_markers)


def should_skip_bot_workflow_initiator(
    actor: str,
    actor_pattern_sources: list[str],
) -> bool:
    actor = (actor or "").strip()
    if not actor.endswith("[bot]"):
        return False
    if matches_any(actor_pattern_sources, actor):
        return False
    return True


def determine_trigger_context(
    inputs: TriggerInputs,
    fetch_pr_details: Callable[[str, str], tuple[str, str]],
    pattern_sources: list[str] | None = None,
    actor_pattern_sources: list[str] | None = None,
    actor_review_markers: list[str] | None = None,
) -> TriggerDecision:
    pattern_sources = pattern_sources or list(DEFAULT_PATTERNS)
    actor_pattern_sources = actor_pattern_sources or list(DEFAULT_ACTOR_PATTERNS)
    actor_review_markers = actor_review_markers or list(ACTOR_REVIEW_MARKERS)

    should_run = False
    selected_pr = inputs.pr_number or ""
    reason = f"{inputs.event}:{inputs.action}" if inputs.action else inputs.event

    if inputs.event == "pull_request":
        should_run = inputs.pr_author.lower() not in AUTOMATION_AUTHORS
        if not should_run:
            reason = "pull_request:bot_pr"
    elif inputs.event == "issue_comment":
        if not inputs.issue_pr_url:
            reason = "issue_comment:not_pr"
        else:
            selected_pr = inputs.issue_number
            head_owner, author_login = fetch_pr_details(inputs.repo_full, inputs.issue_number)
            if author_login.lower() in AUTOMATION_AUTHORS:
                reason = "issue_comment:bot_pr"
            elif inputs.repo_owner and head_owner and head_owner.lower() != inputs.repo_owner.lower():
                reason = "issue_comment:external_pr"
            elif not head_owner:
                reason = "issue_comment:unknown_owner"
            elif matches_any(pattern_sources, inputs.comment_body) or matches_actor_review(
                inputs.comment_author,
                inputs.comment_body,
                actor_pattern_sources,
                actor_review_markers,
            ):
                should_run = True
                reason = "issue_comment:ai_review"
            else:
                reason = "issue_comment:no_match"
    elif inputs.event in {"pull_request_review", "pull_request_review_comment"}:
        selected_pr = inputs.pr_number
        event_prefix = inputs.event
        if not selected_pr:
            reason = f"{event_prefix}:no_pr"
        else:
            head_owner, author_login = fetch_pr_details(inputs.repo_full, selected_pr)
            if author_login.lower() in AUTOMATION_AUTHORS:
                reason = f"{event_prefix}:bot_pr"
            elif inputs.repo_owner and head_owner and head_owner.lower() != inputs.repo_owner.lower():
                reason = f"{event_prefix}:external_pr"
            elif not head_owner:
                reason = f"{event_prefix}:unknown_owner"
            else:
                actor = inputs.review_author if inputs.event == "pull_request_review" else inputs.comment_author
                body = inputs.review_body if inputs.event == "pull_request_review" else inputs.comment_body
                if (
                    matches_any(actor_pattern_sources, actor)
                    or matches_any(pattern_sources, body)
                    or matches_actor_review(
                        actor,
                        body,
                        actor_pattern_sources,
                        actor_review_markers,
                    )
                ):
                    should_run = True
                    reason = f"{event_prefix}:ai_signal"
                else:
                    reason = f"{event_prefix}:no_match"
    else:
        reason = f"unsupported:{inputs.event}"

    if should_run and inputs.event in {
        "issue_comment",
        "pull_request_review",
        "pull_request_review_comment",
    }:
        if should_skip_bot_workflow_initiator(
            inputs.workflow_actor,
            actor_pattern_sources,
        ):
            should_run = False
            reason = f"{reason}:skip_bot_workflow_initiator"

    return TriggerDecision(
        should_run=should_run,
        pr_number=selected_pr or "",
        reason=reason,
    )


def parse_timestamp(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def build_review_entries(
    comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    review_comments: list[dict[str, Any]],
) -> list[ReviewEntry]:
    entries: list[ReviewEntry] = []
    for comment in comments:
        entries.append(
            ReviewEntry(
                entry_type="comment",
                actor=((comment.get("user") or {}).get("login") or "unknown"),
                body=comment.get("body") or "",
                created_at=comment.get("created_at") or comment.get("updated_at") or "",
                url=comment.get("html_url") or comment.get("url") or "",
            )
        )
    for review in reviews:
        links = review.get("_links") or {}
        entries.append(
            ReviewEntry(
                entry_type="review",
                actor=((review.get("user") or {}).get("login") or "unknown"),
                body=review.get("body") or "",
                created_at=(review.get("submitted_at") or review.get("created_at") or review.get("updated_at") or ""),
                url=(
                    review.get("html_url")
                    or ((links.get("html") or {}).get("href"))
                    or ((links.get("pull_request") or {}).get("href"))
                    or ""
                ),
            )
        )
    for review_comment in review_comments:
        entries.append(
            ReviewEntry(
                entry_type="review_comment",
                actor=((review_comment.get("user") or {}).get("login") or "unknown"),
                body=review_comment.get("body") or "",
                created_at=(review_comment.get("created_at") or review_comment.get("updated_at") or ""),
                url=review_comment.get("html_url") or "",
            )
        )
    return entries


def find_ai_review(
    entries: list[ReviewEntry],
    actor_pattern_sources: list[str] | None = None,
    actor_review_markers: list[str] | None = None,
    current_diff_signature: str = "",
) -> AiReviewMatch | None:
    actor_pattern_sources = actor_pattern_sources or list(DEFAULT_ACTOR_PATTERNS)
    actor_review_markers = actor_review_markers or list(ACTOR_REVIEW_MARKERS)
    sorted_entries = sorted(
        entries,
        key=lambda entry: parse_timestamp(entry.created_at),
        reverse=True,
    )

    for entry in sorted_entries:
        body_source = next(
            (source for source, pattern in AI_REVIEW_PATTERNS if re.search(pattern, entry.body or "", re.IGNORECASE)),
            None,
        )
        actor_matched = matches_any(actor_pattern_sources, entry.actor)
        actor_review_matched = actor_matched and (
            entry.entry_type in {"review", "review_comment"}
            or any(re.search(marker, entry.body or "", re.IGNORECASE) for marker in actor_review_markers)
        )

        if (body_source or actor_review_matched) and not finn_ai_coder_entry_matches_current_diff(
            entry,
            current_diff_signature,
            body_source,
        ):
            continue

        if body_source or actor_review_matched:
            actor_source = (
                "finn-ai-coder" if re.search(r"^finn-ai-coder\[bot\]$", entry.actor, re.IGNORECASE) else "finn-codex"
            )
            return AiReviewMatch(
                entry_type=entry.entry_type,
                actor=entry.actor,
                source=body_source or actor_source,
                created_at=entry.created_at,
                url=entry.url,
            )

    return None


def finn_ai_coder_entry_matches_current_diff(
    entry: ReviewEntry,
    current_diff_signature: str,
    body_source: str | None = None,
) -> bool:
    actor = (entry.actor or "").casefold()
    is_finn_ai_coder_actor = actor == finn_ai_metadata.LOGICAL_REVIEWER_LOGIN.casefold()
    is_metadata_author = actor == finn_ai_metadata.METADATA_AUTHOR_LOGIN.casefold()
    if body_source == "finn-ai-coder" and not is_metadata_author:
        return False
    if body_source == "finn-ai-coder":
        metadata = finn_ai_metadata.extract_metadata(entry.body or "")
        if metadata is None:
            return False
        if metadata.get("schema") != "finn-ai-coder-review-metadata/v1":
            return False
        if current_diff_signature and finn_ai_metadata.metadata_diff_value(metadata) != current_diff_signature:
            return False
        return finn_ai_metadata.metadata_normalized_verdict(metadata) in {
            "approved",
            "requested_changes",
        }
    if not is_metadata_author and not is_finn_ai_coder_actor:
        return True
    return is_finn_ai_coder_actor and not current_diff_signature


def extract_metadata_gate_decision(text: str) -> dict[str, Any]:
    prefixes = ("CODEX_DECISION::", "METADATA_DECISION::")
    marker_pattern = re.compile("|".join(re.escape(prefix) for prefix in prefixes))
    decoder = json.JSONDecoder()

    for marker in marker_pattern.finditer(text):
        candidate = text[marker.end() :].lstrip()
        try:
            decision, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decision, dict):
            return decision
    raise ValueError("Metadata decision JSON not found in model output.")


def normalize_whitespace(value: str) -> str:
    return " ".join((value or "").strip().split())


def note_has_case_variant(note: str, title: str) -> bool:
    note_clean = normalize_whitespace(note)
    title_clean = normalize_whitespace(title)
    if not note_clean or not title_clean:
        return False
    if title_clean in note_clean:
        return False
    return title_clean.casefold() in note_clean.casefold()


def checklist_pass(items: list[dict[str, Any]]) -> bool:
    return all(item.get("checked") and item.get("satisfied") for item in items)


def format_ai_label(source: str) -> str:
    return source.strip().capitalize() if source else "AI"


def build_metadata_gate_comment(
    decision: dict[str, Any],
    pr_title: str,
    ai_review: AiReviewState,
) -> tuple[str, str]:
    title_grammar = decision.get("titleGrammar", "fail")
    body_grammar = decision.get("bodyGrammar", "fail")
    imperative = decision.get("imperativeTitle", "fail")
    checklist = decision.get("checklist", [])
    notes = decision.get("notes") or []

    capitalization_override = False
    if imperative == "fail" and notes and pr_title:
        capitalization_override = any(note_has_case_variant(note, pr_title) for note in notes)
        if capitalization_override:
            imperative = "pass"

    overall = (
        "pass"
        if (title_grammar == "pass" and body_grammar == "pass" and imperative == "pass" and checklist_pass(checklist))
        else "fail"
    )

    def format_status(label: str, value: str) -> str:
        icon = "✅" if value == "pass" else "❌"
        return f"{icon} **{label}** — {value}"

    summary_lines = [
        format_status("Title grammar", title_grammar),
        format_status("Imperative title", imperative)
        + (" (model suggestion overridden - capitalization change only)" if capitalization_override else ""),
        format_status("Body grammar", body_grammar),
    ]

    if ai_review.present:
        review_label = f"{format_ai_label(ai_review.source)} review"
        if ai_review.actor:
            review_label += f" by @{ai_review.actor}"
        if ai_review.created_at:
            review_label += f" on {ai_review.created_at}"
        if ai_review.url:
            review_label = f"[{review_label}]({ai_review.url})"
        summary_lines.append(f"✅ **AI review** — {review_label}")
    else:
        summary_lines.append(
            "❌ **AI review** — missing. Comment exactly `/review` on the PR to run an automated review."
        )

    for item in checklist:
        name = item.get("item", "unknown")
        checked = item.get("checked")
        satisfied = item.get("satisfied")
        reason = item.get("reason") or ""
        icon = "✅" if (checked and satisfied) else "❌"
        summary_lines.append(
            f"{icon} **Checklist: {name}** — checked={checked} satisfied={satisfied}. {reason}".strip()
        )

    if notes:
        summary_lines.extend(["", "**Notes:**"])
        summary_lines.extend(f"- {note}" for note in notes)

    status_line = "🚫 **PR metadata gate failed**" if overall == "fail" else "✅ **PR metadata gate passed**"
    comment_lines = [COMMENT_MARKER, status_line, "", *summary_lines]
    return (overall, "\n".join(comment_lines).strip() + "\n")


def evaluate_metadata_gate_output(
    output_text: str,
    pr_title: str,
    ai_review: AiReviewState,
) -> EvaluationResult:
    decision = extract_metadata_gate_decision(output_text)
    overall_status, comment_text = build_metadata_gate_comment(
        decision=decision,
        pr_title=pr_title,
        ai_review=ai_review,
    )
    return EvaluationResult(
        overall_status=overall_status,
        comment_text=comment_text,
    )


def find_existing_gate_comment(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    for comment in comments:
        actor = ((comment.get("user") or {}).get("login") or "").strip()
        body = comment.get("body") or ""
        if actor == "github-actions[bot]" and COMMENT_MARKER in body:
            return comment
    return None


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(f"{name} is required")
    return value


def append_key_values(path: Path, values: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, raw_value in values.items():
            value = "" if raw_value is None else str(raw_value)
            if "\n" in value:
                delimiter = "__PR_METADATA_GATE_EOF__"
                while delimiter in value:
                    delimiter += "_X"
                handle.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                handle.write(f"{key}={value}\n")


def write_github_output(values: dict[str, Any]) -> None:
    append_key_values(Path(require_env("GITHUB_OUTPUT")), values)


def write_github_env(values: dict[str, Any]) -> None:
    append_key_values(Path(require_env("GITHUB_ENV")), values)


def command_determine_context(_: argparse.Namespace) -> int:
    api = GitHubApi(require_env("GH_TOKEN"))
    decision = determine_trigger_context(
        TriggerInputs(
            event=require_env("EVENT_NAME"),
            action=os.environ.get("EVENT_ACTION", ""),
            pr_number=os.environ.get("PR_NUMBER_PR", ""),
            pr_author=os.environ.get("PR_AUTHOR", ""),
            issue_number=os.environ.get("ISSUE_NUMBER", ""),
            issue_pr_url=os.environ.get("ISSUE_PR_URL", ""),
            comment_body=os.environ.get("COMMENT_BODY", ""),
            comment_author=os.environ.get("COMMENT_AUTHOR", ""),
            review_body=os.environ.get("REVIEW_BODY", ""),
            review_author=os.environ.get("REVIEW_AUTHOR", ""),
            repo_full=os.environ.get("REPO_FULL", ""),
            repo_owner=os.environ.get("REPO_OWNER", ""),
            workflow_actor=os.environ.get("GITHUB_ACTOR", ""),
        ),
        fetch_pr_details=api.safe_fetch_pr_details,
        pattern_sources=load_pattern_sources(
            os.environ.get("AI_REVIEW_PATTERNS"),
            DEFAULT_PATTERNS,
        ),
        actor_pattern_sources=load_pattern_sources(
            os.environ.get("AI_REVIEW_ACTOR_PATTERNS"),
            DEFAULT_ACTOR_PATTERNS,
        ),
    )
    payload = {
        "should_run": "true" if decision.should_run else "false",
        "pr_number": decision.pr_number,
        "reason": decision.reason,
    }
    print(json.dumps(payload))
    write_github_output(payload)
    if decision.pr_number:
        write_github_env({"PR_NUMBER": decision.pr_number})
    return 0


def command_fetch_pr_data(_: argparse.Namespace) -> int:
    api = GitHubApi(require_env("GH_TOKEN"))
    repository = require_env("GITHUB_REPOSITORY")
    pr_number = require_env("PR_NUMBER")
    pr_data = api.request_json("GET", f"repos/{repository}/pulls/{pr_number}")
    write_github_output(
        {
            "title": pr_data.get("title") or "",
            "body": pr_data.get("body") or "",
            "author": ((pr_data.get("user") or {}).get("login") or ""),
            "pr_url": pr_data.get("html_url") or "",
        }
    )
    return 0


def command_detect_ai_review(_: argparse.Namespace) -> int:
    pr_number = os.environ.get("PR_NUMBER", "")
    if not pr_number:
        print("No pull request number available for AI review detection.")
        write_github_output(
            {
                "has_ai_review": "false",
                "ai_review_source": "",
                "ai_review_type": "",
                "ai_review_actor": "",
                "ai_review_url": "",
                "ai_review_created_at": "",
            }
        )
        return 0

    api = GitHubApi(require_env("GH_TOKEN"))
    repository = require_env("GITHUB_REPOSITORY")
    metadata_api = finn_ai_metadata.GitHubApi(require_env("GH_TOKEN"))
    current_diff_signature = finn_ai_metadata.diff_signature(
        finn_ai_metadata.fetch_pr_diff(metadata_api, repository, pr_number)
    )
    comments = api.paginate(f"repos/{repository}/issues/{pr_number}/comments")
    reviews = api.paginate(f"repos/{repository}/pulls/{pr_number}/reviews")
    review_comments = api.paginate(f"repos/{repository}/pulls/{pr_number}/comments")
    match = find_ai_review(
        build_review_entries(comments, reviews, review_comments),
        actor_pattern_sources=load_pattern_sources(
            os.environ.get("AI_REVIEW_ACTOR_PATTERNS"),
            DEFAULT_ACTOR_PATTERNS,
        ),
        current_diff_signature=current_diff_signature,
    )
    if match is None:
        print("No AI review detected in PR timeline.")
        write_github_output(
            {
                "has_ai_review": "false",
                "ai_review_source": "",
                "ai_review_type": "",
                "ai_review_actor": "",
                "ai_review_url": "",
                "ai_review_created_at": "",
            }
        )
        return 0

    print(f"Detected {match.source} {match.entry_type} from @{match.actor} at {match.created_at}")
    write_github_output(
        {
            "has_ai_review": "true",
            "ai_review_source": match.source,
            "ai_review_type": match.entry_type,
            "ai_review_actor": match.actor,
            "ai_review_url": match.url,
            "ai_review_created_at": match.created_at,
        }
    )
    return 0


def command_evaluate_codex(_: argparse.Namespace) -> int:
    output_file = Path(require_env("OUTPUT_FILE"))
    if not output_file.exists():
        raise RuntimeError("Codex output file not found.")

    evaluation = evaluate_metadata_gate_output(
        output_text=output_file.read_text(encoding="utf-8"),
        pr_title=os.environ.get("PR_TITLE", ""),
        ai_review=AiReviewState(
            present=(os.environ.get("AI_REVIEW_STATUS", "").lower() == "true"),
            source=os.environ.get("AI_REVIEW_SOURCE", ""),
            actor=os.environ.get("AI_REVIEW_ACTOR", ""),
            url=os.environ.get("AI_REVIEW_URL", ""),
            created_at=os.environ.get("AI_REVIEW_CREATED_AT", ""),
        ),
    )

    comment_path = Path(require_env("RUNNER_TEMP")) / "codex-pr-metadata-comment.md"
    comment_path.write_text(evaluation.comment_text, encoding="utf-8")
    write_github_output(
        {
            "overall_status": evaluation.overall_status,
            "comment_path": str(comment_path),
        }
    )
    return 0


def command_upsert_comment(_: argparse.Namespace) -> int:
    repository = require_env("GITHUB_REPOSITORY")
    pr_number = require_env("PR_NUMBER")
    comment_path = Path(require_env("COMMENT_BODY_PATH"))
    body = comment_path.read_text(encoding="utf-8")
    api = GitHubApi(require_env("GH_TOKEN"))

    comments = api.paginate(f"repos/{repository}/issues/{pr_number}/comments")
    existing = find_existing_gate_comment(comments)
    if existing:
        updated = api.request_json(
            "PATCH",
            f"repos/{repository}/issues/comments/{existing['id']}",
            payload={"body": body},
        )
        comment_url = updated.get("html_url") or existing.get("html_url") or ""
    else:
        created = api.request_json(
            "POST",
            f"repos/{repository}/issues/{pr_number}/comments",
            payload={"body": body},
        )
        comment_url = created.get("html_url") or ""

    if comment_url:
        print(f"Metadata gate comment posted at {comment_url}")
    else:
        print("Metadata gate comment posted, but URL could not be determined.")
    write_github_output({"comment_url": comment_url})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PR metadata gate helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "determine-context",
        "fetch-pr-data",
        "detect-ai-review",
        "evaluate-codex",
        "upsert-comment",
    ):
        subparsers.add_parser(command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "determine-context": command_determine_context,
        "fetch-pr-data": command_fetch_pr_data,
        "detect-ai-review": command_detect_ai_review,
        "evaluate-codex": command_evaluate_codex,
        "upsert-comment": command_upsert_comment,
    }
    try:
        return commands[args.command](args)
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
