# SPDX-License-Identifier: Apache-2.0
"""Review GitHub issues against the repository's issue forms."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import yaml
from pydantic import BaseModel, Field, ValidationError

from scripts.docs_review.github_issue_policy import (
    COMMENT_MARKER,
    MODEL_PROMPT,
    Issue,
    IssueJudgement,
    read_issue_forms,
    render_comment,
    requests_english,
    select_issue_form,
    validate_judgement,
)
from scripts.docs_review.github_pilot import (
    IssueReservation,
    PilotError,
    Reservations,
    Revision,
    github_api,
    load_reservations,
    write_reservations,
)
from scripts.docs_review.models import BudgetConfig, ModelSettings
from scripts.docs_review.transport import OpenAICompatibleReviewer, ReviewerError

MAX_ISSUE_INPUT_BYTES = 30_000
MAX_ISSUE_REVIEWS = 2


class CommentAuthor(BaseModel):
    login: str


class IssueComment(BaseModel):
    id: int = Field(gt=0)
    user: CommentAuthor
    body: str


def authorize_issue_reservation(
    reservations: Reservations, reservation: IssueReservation, budget: BudgetConfig
) -> None:
    issue_runs = [
        entry
        for entry in reservations.issue_reservations
        if entry.issue_number == reservation.issue_number
    ]
    day_cost = sum(
        entry.estimated_cost_cents
        for entry in [*reservations.reservations, *reservations.issue_reservations]
        if entry.date_utc == reservation.date_utc
    )
    if len(issue_runs) >= MAX_ISSUE_REVIEWS:
        raise PilotError("Issue review limit reached")
    elif reservation.estimated_cost_cents > budget.max_cost_cents_per_call:
        raise PilotError("Estimated issue call cost exceeds the per-call budget")
    elif (
        sum(entry.estimated_cost_cents for entry in issue_runs)
        + reservation.estimated_cost_cents
        > budget.max_cost_cents_per_pull_request
    ):
        raise PilotError("Issue review budget exceeded")
    elif day_cost + reservation.estimated_cost_cents > budget.max_cost_cents_per_day:
        raise PilotError("Daily review budget exceeded")
    else:
        pass


def find_bot_comment(repository_name: str, issue_number: int) -> IssueComment | None:
    for page in range(1, 11):
        endpoint = f"repos/{repository_name}/issues/{issue_number}/comments?per_page=100&page={page}"
        raw: object = json.loads(github_api(endpoint))
        if not isinstance(raw, list):
            raise PilotError("Could not read issue comments")
        else:
            comments = [IssueComment.model_validate(entry) for entry in raw]
        for comment in comments:
            if (
                comment.user.login == "github-actions[bot]"
                and COMMENT_MARKER in comment.body
            ):
                return comment
            else:
                pass
        if len(comments) < 100:
            return None
        else:
            pass
    raise PilotError("Issue has too many comments to check for an existing bot reply")


def mark_previous_comment_stale(repository_name: str, issue_number: int) -> None:
    existing = find_bot_comment(repository_name, issue_number)
    body = (
        f"{COMMENT_MARKER}\nThe latest automated review could not be completed. "
        "Earlier suggestions may no longer apply; a maintainer can review this issue."
    )
    if existing is None or existing.body == body:
        pass
    else:
        github_api(
            f"repos/{repository_name}/issues/comments/{existing.id}",
            json.dumps({"body": body}),
            method="PATCH",
        )


def save_issue_result(
    repository_name: str,
    reservation: IssueReservation,
    judgement: IssueJudgement | None,
) -> None:
    reservations, content_sha = load_reservations(repository_name)
    matching = [
        entry
        for entry in reservations.issue_reservations
        if entry.run_id == reservation.run_id
        and entry.run_attempt == reservation.run_attempt
        and entry.review_input_sha256 == reservation.review_input_sha256
    ]
    if len(matching) != 1:
        raise PilotError("The issue review reservation could not be located")
    elif judgement is None:
        matching[0].execution = "incomplete"
    else:
        matching[0].execution = "complete"
        matching[0].category = judgement.category
        matching[0].unclear_fields = list(judgement.unclear_fields)
        matching[0].needs_english = judgement.needs_english
    write_reservations(repository_name, reservations, content_sha)


def review_issue() -> None:
    repository_name = os.environ["GITHUB_REPOSITORY"]
    issue_number = int(os.environ["ISSUE_NUMBER"])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository_name):
        raise PilotError("Invalid repository name")
    elif issue_number < 1:
        raise PilotError("Issue number must be positive")
    else:
        pass
    endpoint = f"repos/{repository_name}/issues/{issue_number}"
    issue = Issue.model_validate_json(github_api(endpoint))
    forms = read_issue_forms(Path(os.environ["GITHUB_WORKSPACE"]))
    selected = select_issue_form(issue, forms)
    if issue.state != "open" or issue.pull_request is not None:
        return
    elif selected is None:
        category_hint = "unclassified"
    else:
        category_hint = selected[0]
    form_sources = {
        category: {"source": source, "requests_english": requests_english(form)}
        for category, (form, source) in forms.items()
    }
    issue_text = json.dumps(
        {"title": issue.title, "body": issue.body or ""}, ensure_ascii=False
    )
    if len(issue_text.encode()) > MAX_ISSUE_INPUT_BYTES:
        mark_previous_comment_stale(repository_name, issue_number)
        raise PilotError("Issue input exceeds the configured byte limit")
    else:
        pass
    settings = ModelSettings.model_validate_json(os.environ["DOCS_REVIEW_MODEL"])
    budget = BudgetConfig.model_validate_json(os.environ["DOCS_REVIEW_BUDGET"])
    if settings.api_key_environment_variable != "DOCS_REVIEW_API_KEY":
        raise PilotError("The model must use DOCS_REVIEW_API_KEY")
    elif not os.environ.get("DOCS_REVIEW_API_KEY") or budget.call_cost_cents <= 0:
        raise PilotError("The issue model key or estimated call cost is not configured")
    else:
        pass
    input_hash = hashlib.sha256(
        json.dumps(
            [
                repository_name,
                issue_number,
                issue_text,
                form_sources,
                category_hint,
                MODEL_PROMPT,
                settings.model_dump(),
            ],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    reservations, content_sha = load_reservations(repository_name)
    matching = [
        entry
        for entry in reservations.issue_reservations
        if entry.issue_number == issue_number
        and entry.review_input_sha256 == input_hash
    ]
    completed = next(
        (entry for entry in matching if entry.execution == "complete"), None
    )
    if completed is not None:
        judgement = IssueJudgement(
            category=completed.category,
            unclear_fields=completed.unclear_fields,
            needs_english=completed.needs_english,
        )
    elif matching and os.environ["RETRY_FAILED"] != "true":
        mark_previous_comment_stale(repository_name, issue_number)
        raise PilotError("This issue review was already reserved and is incomplete")
    else:
        reservation = IssueReservation(
            issue_number=issue_number,
            review_input_sha256=input_hash,
            estimated_cost_cents=budget.call_cost_cents,
            date_utc=datetime.now(timezone.utc).date().isoformat(),
            run_id=os.environ["GITHUB_RUN_ID"],
            run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
        )
        try:
            authorize_issue_reservation(reservations, reservation, budget)
        except PilotError:
            mark_previous_comment_stale(repository_name, issue_number)
            raise
        else:
            pass
        reservations.version = 2
        reservations.issue_reservations.append(reservation)
        write_reservations(repository_name, reservations, content_sha)
        try:
            reviewer = OpenAICompatibleReviewer(settings, MODEL_PROMPT)
            raw = reviewer.review(
                f"Trusted issue forms:\n{json.dumps(form_sources)}\n"
                f"Category hint: {category_hint}\n\nUntrusted issue:\n{issue_text}"
            )
            judgement = validate_judgement(raw, forms, category_hint)
        except (ReviewerError, ValidationError, PilotError):
            save_issue_result(repository_name, reservation, None)
            mark_previous_comment_stale(repository_name, issue_number)
            raise PilotError("Issue model review was incomplete")
        else:
            save_issue_result(repository_name, reservation, judgement)
    current = Issue.model_validate_json(github_api(endpoint))
    default_branch = quote(os.environ["DEFAULT_BRANCH"], safe="")
    base = Revision.model_validate_json(
        github_api(f"repos/{repository_name}/commits/{default_branch}")
    )
    if (
        current.state != "open"
        or current.title != issue.title
        or current.body != issue.body
        or current.labels != issue.labels
        or base.sha != os.environ["GITHUB_SHA"]
    ):
        mark_previous_comment_stale(repository_name, issue_number)
        raise PilotError("The issue changed during review; no reply was posted")
    else:
        pass
    existing = find_bot_comment(repository_name, issue_number)
    if judgement.category == "other":
        if existing is None:
            return
        else:
            mark_previous_comment_stale(repository_name, issue_number)
            return
    elif (
        not judgement.unclear_fields
        and not judgement.needs_english
        and existing is None
    ):
        return
    else:
        form = forms[judgement.category][0]
        body = render_comment(
            form, judgement, repository_name, os.environ["GITHUB_SHA"]
        )
    if existing is None:
        posted = IssueComment.model_validate_json(
            github_api(
                f"{endpoint}/comments", json.dumps({"body": body}), method="POST"
            )
        )
    elif existing.body != body:
        posted = IssueComment.model_validate_json(
            github_api(
                f"repos/{repository_name}/issues/comments/{existing.id}",
                json.dumps({"body": body}),
                method="PATCH",
            )
        )
    else:
        return
    latest = Issue.model_validate_json(github_api(endpoint))
    latest_base = Revision.model_validate_json(
        github_api(f"repos/{repository_name}/commits/{default_branch}")
    )
    if (
        latest.state != "open"
        or latest.title != issue.title
        or latest.body != issue.body
        or latest.labels != issue.labels
        or latest_base.sha != os.environ["GITHUB_SHA"]
    ):
        stale_body = (
            f"{COMMENT_MARKER}\nThis issue changed during the automated review; "
            "the earlier suggestions may no longer apply."
        )
        github_api(
            f"repos/{repository_name}/issues/comments/{posted.id}",
            json.dumps({"body": stale_body}),
            method="PATCH",
        )
    else:
        pass
    print("Advisory issue review processed.")


def main() -> int:
    try:
        review_issue()
    except PilotError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (
        KeyError,
        ValueError,
        OSError,
        RuntimeError,
        ValidationError,
        yaml.YAMLError,
    ):
        print(
            "Issue review could not run; check configuration and permissions.",
            file=sys.stderr,
        )
        return 1
    else:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
