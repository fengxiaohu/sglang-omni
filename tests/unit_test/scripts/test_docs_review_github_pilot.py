# SPDX-License-Identifier: Apache-2.0
"""Behavior tests for the maintainer-triggered review pilot."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from scripts.docs_review import github_pilot, github_publish
from scripts.docs_review.github_pilot import (
    PilotError,
    Reservation,
    Reservations,
    authorize_reservation,
)
from scripts.docs_review.models import BudgetConfig, Report
from scripts.docs_review.transport import OpenAICompatibleReviewer, ReviewerError


def test_pull_request_uses_current_base_branch_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def api(endpoint: str) -> str:
        if endpoint.endswith("/pulls/1"):
            return json.dumps(
                {
                    "state": "open",
                    "body": "",
                    "base": {"sha": "a" * 40, "ref": "release/example"},
                    "head": {"sha": "b" * 40},
                }
            )
        else:
            assert endpoint.endswith("/commits/release%2Fexample")
            return json.dumps({"sha": "c" * 40})

    monkeypatch.setattr(github_pilot, "github_api", api)
    pull_request = github_pilot.fetch_pull_request("owner/repo", 1)
    assert pull_request.base.sha == "c" * 40
    assert pull_request.head.sha == "b" * 40


def test_reservations_bound_duplicates_followups_and_daily_spend() -> None:
    budget = BudgetConfig(
        max_cost_cents_per_call=25,
        max_cost_cents_per_pull_request=50,
        max_cost_cents_per_day=50,
        call_cost_cents=25,
    )
    first = Reservation(
        pull_request_number=1,
        head_commit_sha="a" * 40,
        review_input_sha256="b" * 64,
        estimated_cost_cents=25,
        date_utc="2026-11-06",
        run_id="1",
    )
    second = first.model_copy(update={"review_input_sha256": "c" * 64, "run_id": "2"})
    other = first.model_copy(update={"pull_request_number": 2, "run_id": "3"})
    reservations = Reservations(reservations=[first])
    with pytest.raises(PilotError, match="already reserved"):
        authorize_reservation(reservations, first, budget, False)
    authorize_reservation(reservations, first, budget, True)
    completed = first.model_copy(update={"execution": "complete"})
    with pytest.raises(PilotError, match="already reserved"):
        authorize_reservation(
            Reservations(reservations=[completed]), first, budget, True
        )
    with pytest.raises(PilotError, match="follow-up"):
        authorize_reservation(reservations, second, budget, False)
    authorize_reservation(reservations, second, budget, True)
    reservations.reservations.append(second)
    with pytest.raises(PilotError, match="Daily budget"):
        authorize_reservation(reservations, other, budget, False)
    third = first.model_copy(update={"review_input_sha256": "d" * 64, "run_id": "4"})
    with pytest.raises(PilotError, match="review limit"):
        authorize_reservation(reservations, third, budget, True)


def create_repository(root: Path) -> tuple[str, str]:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Review Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "review@example.com"],
        check=True,
    )
    (root / "README.md").write_text("Base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "Base"], check=True)
    base = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    (root / "docs" / "api_reference").mkdir(parents=True)
    (root / "docs" / "api_reference" / "demo.md").write_text(
        "# API\nThe response is WAV.\n", encoding="utf-8"
    )
    (root / "demo.py").write_text('"""Return raw PCM."""\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "Head"], check=True)
    head = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    return base, head


def configure_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: bool
) -> tuple[str, str, bytes]:
    repository = tmp_path / "repository"
    base, head = create_repository(repository)
    report_key = Fernet.generate_key()
    variables = {
        "GITHUB_REPOSITORY": "owner/repo",
        "PULL_REQUEST_NUMBER": "1",
        "GITHUB_WORKSPACE": str(repository),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "OFFLINE_REVIEW": str(offline).lower(),
        "FOLLOW_UP": "false",
        "INCLUDE_PR_BODY": "false",
        "DOCS_REVIEW_REPORT_KEY": report_key.decode(),
        "DOCS_REVIEW_API_KEY": "test-only-key",
        "DOCS_REVIEW_MODEL": json.dumps(
            {
                "model_name": "gpt-6.1-terra",
                "endpoint_url": "https://example.invalid/v1/chat/completions",
                "max_output_tokens": 8192,
                "api_key_environment_variable": "DOCS_REVIEW_API_KEY",
            }
        ),
        "DOCS_REVIEW_BUDGET": json.dumps(
            {
                "max_cost_cents_per_call": 25,
                "max_cost_cents_per_pull_request": 50,
                "max_cost_cents_per_day": 200,
                "call_cost_cents": 25,
            }
        ),
    }
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        github_pilot, "fetch_revision", lambda repository, revision: None
    )
    return base, head, report_key


def test_offline_report_is_encrypted_and_stale_changes_are_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, head, report_key = configure_run(tmp_path, monkeypatch, offline=True)
    initial = github_pilot.PullRequest(
        state="open", body="Expected answer", base={"sha": base}, head={"sha": head}
    )
    updated = initial.model_copy(update={"head": github_pilot.Revision(sha="f" * 40)})
    responses = iter([initial, updated])
    monkeypatch.setattr(
        github_pilot, "fetch_pull_request", lambda name, number: next(responses)
    )
    github_pilot.run_github_review()
    encrypted = (tmp_path / "docs-review" / "report.fernet").read_bytes()
    assert b"The response is WAV" not in encrypted
    report = json.loads(Fernet(report_key).decrypt(encrypted))
    assert report["stale"] is True
    assert report["report"]["execution"] == "incomplete"
    assert report["input_bundle"]["pull_request_text"] == ""
    assert "stale=true" in (tmp_path / "outputs").read_text(encoding="utf-8")


def test_failed_model_call_is_reserved_before_invocation_and_not_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, head, report_key = configure_run(tmp_path, monkeypatch, offline=False)
    pull_request = github_pilot.PullRequest(
        state="open", body="Expected answer", base={"sha": base}, head={"sha": head}
    )
    monkeypatch.setattr(
        github_pilot, "fetch_pull_request", lambda name, number: pull_request
    )
    reservations = Reservations()
    monkeypatch.setattr(
        github_pilot, "load_reservations", lambda name: (reservations, "a" * 40)
    )

    def save_reservation(
        name: str, ledger: Reservations, sha: str, reservation: Reservation
    ) -> None:
        assert sha == "a" * 40
        ledger.reservations.append(reservation)

    def fail_model(reviewer: OpenAICompatibleReviewer, bundle_text: str) -> str:
        assert len(reservations.reservations) == 1
        raise ReviewerError("Model unavailable")

    monkeypatch.setattr(github_pilot, "save_reservation", save_reservation)
    monkeypatch.setattr(
        github_pilot, "write_reservations", lambda name, ledger, sha: None
    )
    monkeypatch.setattr(OpenAICompatibleReviewer, "review", fail_model)
    github_pilot.run_github_review()
    encrypted = (tmp_path / "docs-review" / "report.fernet").read_bytes()
    report = json.loads(Fernet(report_key).decrypt(encrypted))
    assert report["report"]["execution"] == "incomplete"
    assert reservations.reservations[0].execution == "incomplete"
    with pytest.raises(PilotError, match="already reserved"):
        github_pilot.run_github_review()
    assert len(reservations.reservations) == 1


def test_selected_findings_are_published_and_marked_stale_if_head_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, head, report_key = configure_run(tmp_path, monkeypatch, offline=True)
    original = github_pilot.PullRequest(
        state="open", body="", base={"sha": base}, head={"sha": head}
    )
    monkeypatch.setattr(
        github_pilot, "fetch_pull_request", lambda name, number: original
    )
    github_pilot.run_github_review()
    report_path = tmp_path / "docs-review" / "report.fernet"
    private_report = json.loads(Fernet(report_key).decrypt(report_path.read_bytes()))
    private_report["report"]["execution"] = "complete"
    private_report["report"]["outcome"] = "findings"
    private_report["report"]["findings"] = [
        {
            "rule_id": "API_CONTRACT",
            "file_path": "docs/api_reference/demo.md",
            "line_start": 2,
            "line_end": 2,
            "quote": "The response is WAV.",
            "claim": "@someone documentation says WAV, but code returns PCM.",
            "evidence_file_path": "demo.py",
            "evidence_line_start": 1,
            "evidence_line_end": 1,
            "evidence_quote": "Return raw PCM.",
            "impact": "Clients decode the wrong format.",
            "suggested_fix": "Document PCM.",
            "change_relation": "introduced",
            "certainty": "supported",
        }
    ]
    ciphertext = Fernet(report_key).encrypt(json.dumps(private_report).encode())
    monkeypatch.setenv("REVIEW_RUN_ID", "123")
    monkeypatch.setenv("FINDING_NUMBERS", "1")
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    posted: list[tuple[str, str]] = []

    def api(endpoint: str, body: str | None = None, method: str = "PUT") -> str:
        if endpoint.endswith("/actions/runs/123"):
            return json.dumps(
                {
                    "event": "workflow_dispatch",
                    "path": ".github/workflows/docs-review-pilot.yml",
                    "status": "completed",
                    "head_branch": "main",
                }
            )
        else:
            assert body is not None
            posted.append((method, json.loads(body)["body"]))
            return json.dumps({"id": 42})

    def download(
        arguments: list[str], **kwargs: bool
    ) -> subprocess.CompletedProcess[str]:
        (tmp_path / "review-to-publish" / "report.fernet").write_bytes(ciphertext)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    updated = original.model_copy(update={"head": github_pilot.Revision(sha="f" * 40)})
    revisions = iter([original, updated])
    monkeypatch.setattr(github_publish, "github_api", api)
    monkeypatch.setattr(github_publish.subprocess, "run", download)
    monkeypatch.setattr(
        github_publish, "fetch_pull_request", lambda name, number: next(revisions)
    )
    github_publish.publish_selected_findings()
    assert [method for method, body in posted] == ["POST", "PATCH"]
    assert "@someone" not in posted[0][1]
    assert "claim under review:" in posted[0][1]
    assert head in posted[0][1]
    assert posted[1][1].startswith("**Stale:**")


def test_publish_rejects_invalid_selection() -> None:
    assert "@" not in github_publish.safe_text("@team [click](https://example.invalid)")
    with pytest.raises(PilotError, match="comma-separated"):
        github_publish.selected_findings(Report.model_construct(findings=[]), "1;2")


def test_unit_test_review_delivers_only_an_encrypted_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base, _, report_key = configure_run(tmp_path, monkeypatch, offline=False)
    repository = tmp_path / "repository"
    test_path = "tests/unit_test/test_output.py"
    contract_path = "sglang_omni/api.py"
    for file_path, content in (
        (
            test_path,
            "from sglang_omni.api import speak\n\ndef test_output():\n    assert speak()\n",
        ),
        (contract_path, "def speak():\n    return b'RIFF'\n"),
    ):
        target = repository / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "Test"], check=True)
    head = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    pull_request = github_pilot.PullRequest(
        state="open", body="Expected answer", base={"sha": base}, head={"sha": head}
    )
    monkeypatch.setenv("REVIEW_SCOPE", "unit_tests")
    monkeypatch.setattr(
        github_pilot, "fetch_pull_request", lambda name, number: pull_request
    )
    monkeypatch.setattr(
        github_pilot,
        "github_api",
        lambda endpoint: json.dumps(
            {
                "check_runs": [
                    {
                        "name": "Unit",
                        "head_sha": head,
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }
        ),
    )
    reservations = Reservations()
    monkeypatch.setattr(
        github_pilot, "load_reservations", lambda name: (reservations, "a" * 40)
    )
    monkeypatch.setattr(
        github_pilot, "write_reservations", lambda name, ledger, sha: None
    )

    def review(reviewer: OpenAICompatibleReviewer, bundle_text: str) -> str:
        assert "Test the public contract" in reviewer.prompt_text
        assert '"name": "Unit"' in bundle_text
        assert len(reservations.reservations) == 1
        return json.dumps(
            [
                {
                    "rule_id": "UNIT_TEST_INTERNAL_ASSERTION",
                    "file_path": test_path,
                    "line_start": 4,
                    "line_end": 4,
                    "quote": "assert speak()",
                    "claim": "This assertion accepts any nonempty output.",
                    "evidence_file_path": contract_path,
                    "evidence_line_start": 2,
                    "evidence_line_end": 2,
                    "evidence_quote": "return b'RIFF'",
                    "public_contract": "The caller receives WAV bytes.",
                    "regression": "Returning raw PCM would still satisfy this test.",
                    "impact": "A client expecting WAV cannot parse the audio.",
                    "suggested_fix": "Assert the RIFF header in the response.",
                    "change_relation": "introduced",
                    "certainty": "supported",
                }
            ]
        )

    monkeypatch.setattr(OpenAICompatibleReviewer, "review", review)
    github_pilot.run_github_review()
    encrypted = (tmp_path / "docs-review" / "report.fernet").read_bytes()
    report = json.loads(Fernet(report_key).decrypt(encrypted))["report"]
    assert report["review_scope"] == "unit_tests"
    assert report["outcome"] == "findings"
    assert (
        report["findings"][0]["regression"]
        == "Returning raw PCM would still satisfy this test."
    )
    assert report["check_results"][0]["execution_commit_sha"] == head
    output = capsys.readouterr().out
    assert "findings=1" in output
    assert "Returning raw PCM" not in output
