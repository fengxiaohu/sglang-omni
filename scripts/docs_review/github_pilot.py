# SPDX-License-Identifier: Apache-2.0
"""Run an authorized GitHub documentation review without executing PR code."""

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

from pydantic import BaseModel, Field, ValidationError

from scripts.docs_review.collect import collect_review_bundle, review_input_sha256
from scripts.docs_review.models import (
    BudgetConfig,
    ModelSettings,
    Report,
    ReviewRequest,
)
from scripts.docs_review.policy import read_prompt
from scripts.docs_review.reporting import render_bundle, render_markdown
from scripts.docs_review.review import SystemClock, run_review

STATE_BRANCH = "docs-review-ledger"
STATE_PATH = "docs-review-ledger.json"
MAX_INPUT_BYTES = 100_000
REVIEW_WORKFLOW_PATH = ".github/workflows/docs-review-pilot.yml"


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


class ReviewArtifact(BaseModel):
    pull_request_number: int = Field(gt=0)
    stale: bool
    publishable: bool
    report: Report


class Reservation(BaseModel):
    pull_request_number: int = Field(gt=0)
    head_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    review_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    estimated_cost_cents: int = Field(gt=0)
    date_utc: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    run_id: str = Field(pattern=r"^\d+$")
    run_attempt: int = Field(default=1, gt=0)
    execution: Literal["reserved", "complete", "incomplete"] = "reserved"


class Reservations(BaseModel):
    version: Literal[1] = 1
    reservations: list[Reservation] = Field(default_factory=list)


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
            "message": "Reserve an advisory documentation review",
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
    artifact_directory = output_directory / "artifact"
    artifact_directory.mkdir(parents=True, exist_ok=True)
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
        pull_request_text=pull_request.body or "",
        model_settings=model_settings,
        budget=budget,
        ledger_path=output_directory / "local.sqlite",
    )
    bundle = collect_review_bundle(request)
    if not bundle.reviewed_paths:
        raise PilotError("No changed documentation files were found")
    elif not offline and len(render_bundle(bundle).encode()) > MAX_INPUT_BYTES:
        raise PilotError("Review input exceeds the configured byte limit")
    else:
        pass
    if offline:
        reservation = None
    elif budget is None:
        raise PilotError("The review budget is not configured")
    else:
        prompt_version = hashlib.sha256(read_prompt().encode()).hexdigest()
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
        current.state != "open"
        or current.head.sha != pull_request.head.sha
        or current.base.sha != pull_request.base.sha
    )
    artifact = ReviewArtifact(
        pull_request_number=pull_request_number,
        stale=stale,
        publishable=(
            not offline and not stale and result.report.execution == "complete"
        ),
        report=result.report,
    )
    (artifact_directory / "report.json").write_text(
        artifact.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    notices = [
        f"Pull request #{pull_request_number}",
        "Findings are advisory and selected separately for publication.",
    ]
    if offline:
        notices.append("Collection-only run without a model call.")
    else:
        pass
    if stale:
        notices.append(
            "Stale report. The pull request changed or closed during review."
        )
    else:
        pass
    (artifact_directory / "report.md").write_text(
        "\n\n".join(notices)
        + "\n\n"
        + render_markdown(artifact.report, number_findings=True),
        encoding="utf-8",
    )
    if reservation is not None:
        finish_reservation(repository_name, reservation, result.report.execution)
    else:
        pass
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"execution={result.report.execution}\n")
        output.write(f"stale={str(stale).lower()}\n")
    print(
        f"Advisory review: {result.report.execution}, "
        f"stale={str(stale).lower()}. Report saved."
    )


def main() -> int:
    try:
        run_github_review()
    except PilotError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (KeyError, ValueError, OSError, RuntimeError, ValidationError):
        print(
            "Documentation review could not run; check configuration and permissions.",
            file=sys.stderr,
        )
        return 1
    else:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
