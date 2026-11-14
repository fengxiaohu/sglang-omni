# SPDX-License-Identifier: Apache-2.0
"""Publish maintainer-selected findings from a Docs review run."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from pydantic import BaseModel, Field, ValidationError

from scripts.docs_review.github_pilot import (
    REVIEW_WORKFLOW_PATH,
    PilotError,
    ReviewArtifact,
    fetch_pull_request,
    github_api,
)
from scripts.docs_review.models import Finding, Report


class WorkflowRun(BaseModel):
    event: str
    path: str
    status: str
    conclusion: str | None
    head_branch: str
    run_attempt: int = Field(gt=0)


class PostedComment(BaseModel):
    id: int = Field(gt=0)


def safe_text(value: str) -> str:
    escaped = html.escape(" ".join(value.split())[:2000], quote=True)
    for character in "@[]()*_`~\\":
        escaped = escaped.replace(character, f"&#{ord(character)};")
    return escaped


def format_finding(repository_url: str, head_sha: str, finding: Finding) -> str:
    document_path = quote(finding.file_path, safe="/")
    evidence_path = quote(finding.evidence_file_path, safe="/")
    revision_url = f"{repository_url}/blob/{head_sha}"
    return "\n".join(
        [
            f"- **{safe_text(finding.rule_id)}** — claim under review: {safe_text(finding.claim)}",
            f"  - Document: [{safe_text(finding.file_path)}:{finding.line_start}]({revision_url}/{document_path}#L{finding.line_start})",
            f"  - Evidence: [{safe_text(finding.evidence_file_path)}:{finding.evidence_line_start}]({revision_url}/{evidence_path}#L{finding.evidence_line_start}) — {safe_text(finding.evidence_quote)}",
            f"  - Impact: {safe_text(finding.impact)}",
            f"  - Suggested fix: {safe_text(finding.suggested_fix)}",
        ]
    )


def selected_findings(report: Report, selection: str) -> list[Finding]:
    if not re.fullmatch(r"[1-9][0-9]*(,[1-9][0-9]*)*", selection):
        raise PilotError("Choose one or more comma-separated finding numbers")
    else:
        indices = [int(value) for value in selection.split(",")]
    if len(indices) != len(set(indices)) or any(
        index > len(report.findings) for index in indices
    ):
        raise PilotError("A selected finding number is duplicated or out of range")
    else:
        return [report.findings[index - 1] for index in indices]


def publish_selected_findings() -> None:
    repository_name = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["REVIEW_RUN_ID"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository_name):
        raise PilotError("Invalid repository name")
    elif not re.fullmatch(r"[0-9]+", run_id):
        raise PilotError("Invalid review run number")
    else:
        pass
    run = WorkflowRun.model_validate_json(
        github_api(f"repos/{repository_name}/actions/runs/{run_id}")
    )
    if (
        run.event != "workflow_dispatch"
        or run.path != REVIEW_WORKFLOW_PATH
        or run.status != "completed"
        or run.conclusion != "success"
        or run.head_branch != os.environ["DEFAULT_BRANCH"]
    ):
        raise PilotError("Select a successful default-branch Docs review run")
    else:
        pass
    with TemporaryDirectory(dir=os.environ["RUNNER_TEMP"]) as download_directory:
        completed = subprocess.run(
            [
                "gh",
                "run",
                "download",
                run_id,
                "--repo",
                repository_name,
                "--name",
                f"docs-review-{run_id}-{run.run_attempt}",
                "--dir",
                download_directory,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            raise PilotError("Could not download the Docs review report")
        else:
            artifact = ReviewArtifact.model_validate_json(
                (Path(download_directory) / "report.json").read_bytes()
            )
    report = artifact.report
    if not artifact.publishable or artifact.stale or report.execution != "complete":
        raise PilotError("Select a complete, current, model-backed Docs review")
    else:
        findings = selected_findings(report, os.environ["FINDING_NUMBERS"])
    pull_request = fetch_pull_request(repository_name, artifact.pull_request_number)
    if (
        pull_request.state != "open"
        or pull_request.head.sha != report.head_commit_sha
        or pull_request.base.sha != report.base_commit_sha
    ):
        raise PilotError(
            "The pull request changed since the review; publish is cancelled"
        )
    else:
        pass
    repository_url = f'{os.environ["GITHUB_SERVER_URL"]}/{repository_name}'
    body = "\n\n".join(
        [
            "### Advisory documentation review",
            f"Maintainer-selected findings for commit `{report.head_commit_sha}`. "
            "They apply only to this exact revision; check the current head before acting.",
            *[
                format_finding(repository_url, report.head_commit_sha, finding)
                for finding in findings
            ],
            f"Review run: {repository_url}/actions/runs/{run_id}/attempts/{run.run_attempt}",
        ]
    )
    response = github_api(
        f"repos/{repository_name}/issues/{artifact.pull_request_number}/comments",
        json.dumps({"body": body}),
        method="POST",
    )
    comment = PostedComment.model_validate_json(response)
    current = fetch_pull_request(repository_name, artifact.pull_request_number)
    if (
        current.state != "open"
        or current.head.sha != report.head_commit_sha
        or current.base.sha != report.base_commit_sha
    ):
        github_api(
            f"repos/{repository_name}/issues/comments/{comment.id}",
            json.dumps(
                {
                    "body": "**Stale:** The pull request changed or closed during publication.\n\n"
                    + body
                }
            ),
            method="PATCH",
        )
    else:
        pass
    print("Selected advisory findings published.")


def main() -> int:
    try:
        publish_selected_findings()
    except PilotError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (KeyError, ValueError, OSError, RuntimeError, ValidationError):
        print(
            "Could not publish the selected findings; check the run and configuration.",
            file=sys.stderr,
        )
        return 1
    else:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
