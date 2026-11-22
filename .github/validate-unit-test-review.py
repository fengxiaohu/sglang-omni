"""Verify the unit-test reviewer against frozen fork fixtures."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from scripts.docs_review.github_pilot import (
    Reservation,
    fetch_pull_request,
    fetch_revision,
    finish_reservation,
    model_settings_for_scope,
    save_reservation_with_retry,
)
from scripts.docs_review.models import BudgetConfig, ReviewRequest
from scripts.docs_review.review import SystemClock, prepare_review, run_review

BASE = "cddb0d4be6de0051645d5dba38ec4f412d498250"
CLI_TEST = "tests/unit_test/config/test_cli_config.py"
CLI_CONTRACT = "sglang_omni/cli/config.py"
ROUTER_TEST = "tests/unit_test/router/test_weight_update_contract.py"
ROUTER_CONTRACT = "sglang_omni_router/python/control_plane.py"
CLI_SOURCES = [CLI_CONTRACT, "sglang_omni/config/manager.py", "sglang_omni/config/sources.py"]
ROUTER_SOURCES = [
    ROUTER_CONTRACT,
    "sglang_omni_router/python/app.py",
    "sglang_omni_router/python/config.py",
    "sglang_omni_router/python/worker.py",
]
CASES = {
    34: ("e962bb823d399b16fbefc9807ad55d7fa40aed57", CLI_TEST, CLI_SOURCES, 1),
    33: ("ec9d71ed38bd523bd96d0e695d2de7db1ae82b76", CLI_TEST, CLI_SOURCES, 0),
    37: ("7514ae0c5386e2673a5d688b2de9eb06414bf88d", ROUTER_TEST, ROUTER_SOURCES, 1),
    38: ("0bdd04b0fdaff3d85743d57a5a614c82591dcbef", ROUTER_TEST, ROUTER_SOURCES, 0),
}


def main() -> None:
    fixture_number = int(os.environ["FIXTURE_PR"])
    head_sha, test_path, contract_paths, expected_findings = CASES[fixture_number]
    repository = Path(os.environ["GITHUB_WORKSPACE"])
    repository_name = os.environ["GITHUB_REPOSITORY"]
    fixture = fetch_pull_request(repository_name, fixture_number)
    assert fixture.head.sha == head_sha, "The fork fixture changed"
    fetch_revision(repository, f"refs/pull/{fixture_number}/head")

    budget = BudgetConfig.model_validate_json(os.environ["DOCS_REVIEW_BUDGET"])
    settings = model_settings_for_scope("unit_tests", offline=False)
    assert settings is not None
    assert settings.api_key_environment_variable == "DOCS_REVIEW_API_KEY"
    request = ReviewRequest(
        repository=repository,
        base_commit_sha=BASE,
        head_commit_sha=head_sha,
        pull_request_number=fixture_number,
        pull_request_text="",
        review_scope="unit_tests",
        model_settings=settings,
        budget=budget,
        ledger_path=Path(os.environ["RUNNER_TEMP"]) / "acceptance.sqlite",
        include_paths=contract_paths,
    )
    prepared = prepare_review(request)
    assert prepared.bundle.needs_model, prepared.bundle.coverage_limitations
    assert {case.file_path for case in prepared.bundle.unit_test_cases} == {test_path}
    reservation = Reservation(
        pull_request_number=fixture_number,
        head_commit_sha=head_sha,
        review_input_sha256=prepared.input_hash,
        estimated_cost_cents=budget.call_cost_cents,
        date_utc=datetime.now(timezone.utc).date().isoformat(),
        run_id=os.environ["GITHUB_RUN_ID"],
        run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
    )
    save_reservation_with_retry(repository_name, reservation, budget, True)
    report = run_review(request, None, SystemClock(), prepared=prepared).report
    finish_reservation(repository_name, reservation, report.execution)
    results = {
        "fixture": fixture_number,
        "head": head_sha,
        "execution": report.execution,
        "outcome": report.outcome,
        "incomplete_reason": report.incomplete_reason,
        "case_count": len(report.unit_test_cases),
        "reviewed_cases": [case.name for case in report.unit_test_cases if case.reviewed],
        "findings": [finding.model_dump(mode="json") for finding in report.findings],
    }
    print(json.dumps(results))
    assert report.execution == "complete", report.incomplete_reason
    assert len(report.findings) == expected_findings
    for finding in report.findings:
        assert finding.file_path == test_path
        assert finding.evidence_file_path and finding.evidence_quote
        assert finding.public_contract and finding.regression and finding.suggested_fix
    current = fetch_pull_request(repository_name, fixture_number)
    assert current.head.sha == head_sha, "The fork fixture changed during the run"


if __name__ == "__main__":
    main()
