# SPDX-License-Identifier: Apache-2.0
"""Publish maintainer-selected findings from an encrypted review report."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field, ValidationError

from scripts.docs_review.github_pilot import PilotError, fetch_pull_request, github_api
from scripts.docs_review.models import Finding, Report, ReviewScope


class WorkflowRun(BaseModel):
    event: str
    path: str
    status: str
    head_branch: str


class PrivateReport(BaseModel):
    pull_request_number: int = Field(gt=0)
    stale: bool
    report: Report


class PostedComment(BaseModel):
    id: int = Field(gt=0)


def safe_text(value: str) -> str:
    escaped = html.escape(" ".join(value.split())[:2000], quote=True)
    for character in "@[]()*_`~\\":
        escaped = escaped.replace(character, f"&#{ord(character)};")
    return escaped


def format_finding(
    repository_name: str,
    head_sha: str,
    finding: Finding,
    review_scope: ReviewScope = "docs",
) -> str:
    subject_path = quote(finding.file_path, safe="/")
    evidence_path = quote(finding.evidence_file_path, safe="/")
    repository_url = f"https://github.com/{repository_name}/blob/{head_sha}"
    subject_label = "Test" if review_scope == "unit_tests" else "Document"
    claim = safe_text(finding.claim)
    if review_scope == "docs":
        claim = f"claim under review: {claim}"
    else:
        pass
    lines = [
        f"- **{safe_text(finding.rule_id)}** — {claim}",
        f"  - {subject_label}: [{safe_text(finding.file_path)}:{finding.line_start}]({repository_url}/{subject_path}#L{finding.line_start})",
        f"  - Evidence: [{safe_text(finding.evidence_file_path)}:{finding.evidence_line_start}]({repository_url}/{evidence_path}#L{finding.evidence_line_start}) — {safe_text(finding.evidence_quote)}",
    ]
    if review_scope == "unit_tests":
        lines.extend(
            [
                f"  - Public contract: {safe_text(finding.public_contract)}",
                f"  - Regression: {safe_text(finding.regression)}",
            ]
        )
        if finding.duplicate_test is not None:
            duplicate = finding.duplicate_test
            duplicate_path = quote(duplicate.file_path, safe="/")
            lines.append(
                f"  - Covering test: [{safe_text(duplicate.file_path)}:{duplicate.line_start}]"
                f"({repository_url}/{duplicate_path}#L{duplicate.line_start})"
            )
        else:
            pass
    else:
        pass
    lines.extend(
        [
            f"  - Impact: {safe_text(finding.impact)}",
            f"  - Suggested fix: {safe_text(finding.suggested_fix)}",
        ]
    )
    return "\n".join(lines)


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
        or run.path != ".github/workflows/docs-review-pilot.yml"
        or run.status != "completed"
        or run.head_branch != os.environ["DEFAULT_BRANCH"]
    ):
        raise PilotError("The selected run is not a completed default-branch review")
    else:
        pass
    output_directory = Path(os.environ["RUNNER_TEMP"]) / "review-to-publish"
    output_directory.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "gh",
            "run",
            "download",
            run_id,
            "--repo",
            repository_name,
            "--name",
            f"docs-review-{run_id}",
            "--dir",
            str(output_directory),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise PilotError("Could not download the encrypted review report")
    else:
        pass
    ciphertext = (output_directory / "report.fernet").read_bytes()
    plaintext = Fernet(os.environ["DOCS_REVIEW_REPORT_KEY"].encode()).decrypt(
        ciphertext
    )
    private_report = PrivateReport.model_validate_json(plaintext)
    report = private_report.report
    if private_report.stale or report.execution != "complete":
        raise PilotError("A stale or incomplete review cannot be published")
    else:
        findings = selected_findings(report, os.environ["FINDING_NUMBERS"])
    pull_request = fetch_pull_request(
        repository_name, private_report.pull_request_number
    )
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
    body = "\n\n".join(
        [
            (
                "### Advisory unit-test contract review"
                if report.review_scope == "unit_tests"
                else "### Advisory documentation review"
            ),
            f"Maintainer-selected findings for commit `{report.head_commit_sha}`. "
            "They apply only to this exact revision; check the current head before acting.",
            *[
                format_finding(
                    repository_name,
                    report.head_commit_sha,
                    finding,
                    report.review_scope,
                )
                for finding in findings
            ],
            f"Review run: https://github.com/{repository_name}/actions/runs/{run_id}",
        ]
    )
    response = github_api(
        f"repos/{repository_name}/issues/{private_report.pull_request_number}/comments",
        json.dumps({"body": body}),
        method="POST",
    )
    comment = PostedComment.model_validate_json(response)
    current = fetch_pull_request(repository_name, private_report.pull_request_number)
    if (
        current.head.sha != report.head_commit_sha
        or current.base.sha != report.base_commit_sha
    ):
        github_api(
            f"repos/{repository_name}/issues/comments/{comment.id}",
            json.dumps(
                {
                    "body": "**Stale:** The pull request changed during publication.\n\n"
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
    except (KeyError, ValueError, OSError, RuntimeError, ValidationError, InvalidToken):
        print(
            "Could not publish the selected findings; check the run and configuration.",
            file=sys.stderr,
        )
        return 1
    else:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
