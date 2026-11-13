# SPDX-License-Identifier: Apache-2.0
"""Run an authorized GitHub review from frozen pull request revisions."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from cryptography.fernet import Fernet
from pydantic import BaseModel, Field, ValidationError

from scripts.docs_review.collect import collect_review_bundle, review_input_sha256
from scripts.docs_review.models import (
    BudgetConfig,
    CheckFreshness,
    CheckRecord,
    CheckStatus,
    ModelSettings,
    ReviewRequest,
    ReviewScope,
)
from scripts.docs_review.policy import read_prompt
from scripts.docs_review.reporting import render_bundle, render_markdown
from scripts.docs_review.review import SystemClock, run_review

STATE_BRANCH = "codex/docs-review-ledger"
STATE_PATH = "docs-review-ledger.json"
MAX_INPUT_BYTES = 100_000


class PilotError(Exception):
    """A safe-to-display operational failure."""


class Revision(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    ref: str = ""


class PullRequest(BaseModel):
    state: Literal["open", "closed"]
    body: str | None
    base: Revision
    head: Revision


class GitHubCheckRun(BaseModel):
    name: str
    head_sha: str
    status: str
    conclusion: str | None


class GitHubCheckRuns(BaseModel):
    check_runs: list[GitHubCheckRun]


GITHUB_CHECK_STATUSES = {
    "success": CheckStatus.PASSED,
    "failure": CheckStatus.FAILED,
    "skipped": CheckStatus.SKIPPED,
    "cancelled": CheckStatus.CANCELLED,
    "timed_out": CheckStatus.TIMED_OUT,
}


def fetch_unit_test_checks(
    repository_name: str, head_commit_sha: str
) -> list[CheckRecord]:
    try:
        response = github_api(
            f"repos/{repository_name}/commits/{head_commit_sha}/check-runs?per_page=100"
        )
        checks = GitHubCheckRuns.model_validate_json(response).check_runs
    except (PilotError, ValidationError):
        return []
    else:
        return [
            CheckRecord(
                name=check.name,
                status=(
                    GITHUB_CHECK_STATUSES.get(
                        check.conclusion or "", CheckStatus.UNKNOWN
                    )
                    if check.status == "completed"
                    else CheckStatus.PENDING
                ),
                freshness=(
                    CheckFreshness.CURRENT
                    if check.head_sha == head_commit_sha
                    else CheckFreshness.STALE
                ),
                execution_commit_sha=check.head_sha,
                coverage="GitHub check summary. Per-test coverage is unknown.",
            )
            for check in checks
        ]


class Reservation(BaseModel):
    pull_request_number: int = Field(gt=0)
    head_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    review_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    estimated_cost_cents: int = Field(gt=0)
    date_utc: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    run_id: str = Field(pattern=r"^\d+$")
    run_attempt: int = Field(default=1, gt=0)
    execution: Literal["reserved", "complete", "incomplete"] = "reserved"


class IssueReservation(BaseModel):
    issue_number: int = Field(gt=0)
    review_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    estimated_cost_cents: int = Field(gt=0)
    date_utc: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    run_id: str = Field(pattern=r"^\d+$")
    run_attempt: int = Field(default=1, gt=0)
    execution: Literal["reserved", "complete", "incomplete"] = "reserved"
    category: Literal["bug", "feature", "other"] = "other"
    unclear_fields: list[str] = Field(default_factory=list)
    needs_english: bool = False


class Reservations(BaseModel):
    version: Literal[1, 2] = 1
    reservations: list[Reservation] = Field(default_factory=list)
    issue_reservations: list[IssueReservation] = Field(default_factory=list)


class GitHubContent(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    content: str
    encoding: Literal["base64"]


def github_api(endpoint: str, body: str | None = None, method: str = "PUT") -> str:
    arguments = ["gh", "api", endpoint]
    if body is None:
        input_text = None
    else:
        arguments.extend(["--method", method, "--input", "-"])
        input_text = body
    completed = subprocess.run(
        arguments, input=input_text, capture_output=True, text=True, check=False
    )
    if completed.returncode:
        status = re.search(r"HTTP (\d{3})", completed.stderr)
        if status is None:
            raise PilotError("GitHub API request failed; check repository permissions")
        else:
            raise PilotError(f"GitHub API request failed (HTTP {status.group(1)})")
    else:
        return completed.stdout


def fetch_pull_request(repository_name: str, pull_request_number: int) -> PullRequest:
    response = github_api(f"repos/{repository_name}/pulls/{pull_request_number}")
    pull_request = PullRequest.model_validate_json(response)
    if not pull_request.base.ref:
        raise PilotError("The pull request base branch is unavailable")
    else:
        base_ref = quote(pull_request.base.ref, safe="")
        base_response = github_api(f"repos/{repository_name}/commits/{base_ref}")
        base_sha = Revision.model_validate_json(base_response).sha
        return pull_request.model_copy(
            update={"base": pull_request.base.model_copy(update={"sha": base_sha})}
        )


def load_reservations(repository_name: str) -> tuple[Reservations, str]:
    endpoint = f"repos/{repository_name}/contents/{STATE_PATH}?ref={STATE_BRANCH}"
    content = GitHubContent.model_validate_json(github_api(endpoint))
    raw = base64.b64decode(content.content, validate=False)
    return Reservations.model_validate_json(raw), content.sha


def authorize_reservation(
    reservations: Reservations,
    reservation: Reservation,
    budget: BudgetConfig,
    follow_up_authorized: bool,
) -> None:
    pull_request_runs = [
        entry
        for entry in reservations.reservations
        if entry.pull_request_number == reservation.pull_request_number
    ]
    day_runs = [
        entry
        for entry in reservations.reservations
        if entry.date_utc == reservation.date_utc
    ]
    matching_runs = [
        entry
        for entry in pull_request_runs
        if entry.review_input_sha256 == reservation.review_input_sha256
    ]
    if matching_runs and (
        not follow_up_authorized
        or any(entry.execution == "complete" for entry in matching_runs)
    ):
        raise PilotError("This exact review was already reserved; see its earlier run")
    elif len(pull_request_runs) >= 2:
        raise PilotError("Pull request review limit reached")
    elif pull_request_runs and not follow_up_authorized:
        raise PilotError("A follow-up review must be explicitly authorized")
    elif reservation.estimated_cost_cents > budget.max_cost_cents_per_call:
        raise PilotError("Estimated call cost exceeds the per-call budget")
    elif (
        sum(entry.estimated_cost_cents for entry in pull_request_runs)
        + reservation.estimated_cost_cents
        > budget.max_cost_cents_per_pull_request
    ):
        raise PilotError("Pull request budget exceeded")
    elif (
        sum(entry.estimated_cost_cents for entry in day_runs)
        + sum(
            entry.estimated_cost_cents
            for entry in reservations.issue_reservations
            if entry.date_utc == reservation.date_utc
        )
        + reservation.estimated_cost_cents
        > budget.max_cost_cents_per_day
    ):
        raise PilotError("Daily budget exceeded")
    else:
        pass


def save_reservation(
    repository_name: str,
    reservations: Reservations,
    content_sha: str,
    reservation: Reservation,
) -> None:
    reservations.reservations.append(reservation)
    write_reservations(repository_name, reservations, content_sha)


def write_reservations(
    repository_name: str, reservations: Reservations, content_sha: str
) -> None:
    content = base64.b64encode(reservations.model_dump_json().encode()).decode()
    body = json.dumps(
        {
            "message": "Record an advisory review reservation",
            "content": content,
            "sha": content_sha,
            "branch": STATE_BRANCH,
        }
    )
    github_api(f"repos/{repository_name}/contents/{STATE_PATH}", body)


def finish_reservation(
    repository_name: str,
    reservation: Reservation,
    execution: Literal["complete", "incomplete"],
) -> None:
    reservations, content_sha = load_reservations(repository_name)
    matching = [
        entry
        for entry in reservations.reservations
        if entry.run_id == reservation.run_id
        and entry.run_attempt == reservation.run_attempt
        and entry.review_input_sha256 == reservation.review_input_sha256
    ]
    if len(matching) != 1:
        raise PilotError("The review reservation could not be located")
    else:
        matching[0].execution = execution
        write_reservations(repository_name, reservations, content_sha)


def fetch_revision(repository: Path, revision: str) -> None:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repository),
            "fetch",
            "--no-tags",
            "origin",
            revision,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise PilotError("Could not fetch a pull request revision")
    else:
        pass


def run_github_review() -> None:
    repository_name = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository_name):
        raise PilotError("Invalid repository name")
    else:
        pass
    pull_request_number = int(os.environ["PULL_REQUEST_NUMBER"])
    if pull_request_number < 1:
        raise PilotError("Pull request number must be positive")
    else:
        pass
    pull_request = fetch_pull_request(repository_name, pull_request_number)
    if pull_request.state != "open":
        raise PilotError("The pull request is not open")
    else:
        pass
    repository = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    fetch_revision(repository, pull_request.base.sha)
    fetch_revision(repository, f"refs/pull/{pull_request_number}/head")
    output_directory = Path(os.environ["RUNNER_TEMP"]) / "docs-review"
    output_directory.mkdir(parents=True, exist_ok=True)
    report_key = os.environ["DOCS_REVIEW_REPORT_KEY"].encode()
    cipher = Fernet(report_key)
    scope_value = os.environ.get("REVIEW_SCOPE") or "docs"
    if scope_value == "unit_tests":
        review_scope: ReviewScope = "unit_tests"
    elif scope_value == "docs":
        review_scope = "docs"
    else:
        raise PilotError("Unknown review scope")
    offline = os.environ["OFFLINE_REVIEW"] == "true"
    if offline:
        model_settings = None
        budget = None
    else:
        model_settings = ModelSettings.model_validate_json(
            os.environ["DOCS_REVIEW_MODEL"]
        )
        budget = BudgetConfig.model_validate_json(os.environ["DOCS_REVIEW_BUDGET"])
        if model_settings.api_key_environment_variable != "DOCS_REVIEW_API_KEY":
            raise PilotError("The model must use DOCS_REVIEW_API_KEY")
        elif not os.environ.get("DOCS_REVIEW_API_KEY"):
            raise PilotError("The model API key is not configured")
        elif budget.call_cost_cents <= 0:
            raise PilotError("The estimated call cost must be positive")
        else:
            pass
    request = ReviewRequest(
        repository=repository,
        base_commit_sha=pull_request.base.sha,
        head_commit_sha=pull_request.head.sha,
        pull_request_number=pull_request_number,
        pull_request_text=(
            pull_request.body or "" if os.environ["INCLUDE_PR_BODY"] == "true" else ""
        ),
        model_settings=model_settings,
        budget=budget,
        ledger_path=output_directory / "local.sqlite",
        review_scope=review_scope,
        include_paths=os.environ.get("UNIT_TEST_CONTEXT_PATHS", "").splitlines(),
        checks=(
            fetch_unit_test_checks(repository_name, pull_request.head.sha)
            if review_scope == "unit_tests"
            else []
        ),
    )
    bundle = collect_review_bundle(request)
    if review_scope == "docs" and not bundle.reviewed_paths:
        raise PilotError("No changed documentation files were found")
    elif bundle.needs_model and len(render_bundle(bundle).encode()) > MAX_INPUT_BYTES:
        if review_scope == "unit_tests":
            request.collection_limitations.append(
                "Review input exceeds the configured byte limit. Select a smaller PR or fewer context files."
            )
            bundle = collect_review_bundle(request)
        else:
            raise PilotError("Review input exceeds the configured byte limit")
    else:
        pass
    if offline or not bundle.needs_model:
        reservation = None
    else:
        if budget is None:
            raise PilotError("The review budget is not configured")
        else:
            prompt_version = hashlib.sha256(
                read_prompt(review_scope).encode()
            ).hexdigest()
            input_hash = review_input_sha256(bundle, model_settings, prompt_version)
            reservation = Reservation(
                pull_request_number=pull_request_number,
                head_commit_sha=pull_request.head.sha,
                review_input_sha256=input_hash,
                estimated_cost_cents=budget.call_cost_cents,
                date_utc=datetime.now(timezone.utc).date().isoformat(),
                run_id=os.environ["GITHUB_RUN_ID"],
                run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
            )
            reservations, content_sha = load_reservations(repository_name)
            authorize_reservation(
                reservations,
                reservation,
                budget,
                os.environ["FOLLOW_UP"] == "true",
            )
            save_reservation(repository_name, reservations, content_sha, reservation)
    result = run_review(request, None, SystemClock())
    current = fetch_pull_request(repository_name, pull_request_number)
    stale = (
        current.head.sha != pull_request.head.sha
        or current.base.sha != pull_request.base.sha
    )
    private_report = {
        "pull_request_number": pull_request_number,
        "stale": stale,
        "input_bundle": result.payload.model_dump(mode="json"),
        "report": result.report.model_dump(mode="json"),
        "markdown": render_markdown(result.report),
    }
    encrypted = cipher.encrypt(json.dumps(private_report).encode())
    (output_directory / "report.fernet").write_bytes(encrypted)
    if reservation is not None:
        finish_reservation(repository_name, reservation, result.report.execution)
    else:
        pass
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"execution={result.report.execution}\n")
        output.write(f"stale={str(stale).lower()}\n")
    if review_scope == "unit_tests":
        print(
            f"Unit-test review: {result.report.execution}, {result.report.outcome}, "
            f"cases={len(result.report.unit_test_cases)}, "
            f"findings={len(result.report.findings)}, "
            f"dropped={result.report.dropped_citation_count}, "
            f"stale={str(stale).lower()}; encrypted report saved."
        )
    else:
        print(
            f"Advisory review: {result.report.execution}, "
            f"stale={str(stale).lower()}; encrypted report saved."
        )


def main() -> int:
    try:
        run_github_review()
    except PilotError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (KeyError, ValueError, OSError, RuntimeError, ValidationError):
        print(
            "Advisory review could not run; check configuration and permissions.",
            file=sys.stderr,
        )
        return 1
    else:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
