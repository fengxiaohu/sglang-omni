# SPDX-License-Identifier: Apache-2.0
"""Check pre-collection sharding, retry reports and timing calibration."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.utils import pytest_partition
from scripts.ci.utils.update_pytest_durations import duration_weights

pytest_plugins = ["pytester"]

REPOSITORY = Path(__file__).resolve().parents[3]
PLUGIN = "scripts.ci.utils.pytest_partition"


@pytest.mark.parametrize(("mode", "size"), [("all", 8), ("accelerator", 2)])
def test_partition_coverage(
    pytester: pytest.Pytester, tmp_path: Path, mode: str, size: int
) -> None:
    marked = tmp_path / "marked"
    marked.mkdir()
    (marked / "conftest.py").write_text("marker = 'accelerator'")
    nested = marked / "nested"
    nested.mkdir()
    files = {
        nested / "child_test.py",
        tmp_path / "test_model.py",
        tmp_path / "test_mark.py",
    }
    for path in files:
        path.touch()
    (tmp_path / "test_mark.py").write_text("pytestmark = pytest.mark.accelerator")
    cold = tmp_path / "cold"
    cold.mkdir()
    (cold / "conftest.py").write_text("raise RuntimeError('unexpected import')")
    cpu_file = cold / "test_cpu.py"
    cpu_file.touch()
    expected = files | {cpu_file} if mode == "all" else files
    model = tmp_path / "weights.json"
    model.write_text('{"test_model.py": 2}')
    config = pytester.parseconfig(
        str(tmp_path),
        "--rootdir",
        str(tmp_path),
        "-p",
        PLUGIN,
        "--auto-partition-id",
        "0",
        "--auto-partition-size",
        str(size),
        "--partition-model-file",
        str(model),
        "--partition-prefilter",
        mode,
    )
    seen: set[Path] = set()
    for partition_id in range(size):
        config.option.auto_partition_id = partition_id
        pytest_partition.pytest_configure(config)
        selected = config.stash[pytest_partition.PARTITION].selected_file_paths
        assert seen.isdisjoint(selected)
        seen.update(selected)
        pytest_partition.pytest_configure(config)
        assert selected == config.stash[pytest_partition.PARTITION].selected_file_paths
        assert pytest_partition.pytest_ignore_collect(cold, config) is (
            None if cpu_file in selected else True
        )
        assert (
            pytest_partition.pytest_ignore_collect(cold / "conftest.py", config) is None
        )
    assert seen == expected


def test_repository_accelerator_coverage(pytester: pytest.Pytester) -> None:
    model = REPOSITORY / ".github/unit_test_accelerator_durations.json"
    root = REPOSITORY / "tests"
    config = pytester.parseconfig(
        str(root),
        "--noconftest",
        "-p",
        PLUGIN,
        "--auto-partition-id",
        "0",
        "--auto-partition-size",
        "1",
        "--partition-model-file",
        str(model),
        "--partition-prefilter",
        "accelerator",
    )
    pytest_partition.pytest_configure(config)
    selected = config.stash[pytest_partition.PARTITION].selected_file_paths
    declared = {
        path
        for path in root.rglob("*.py")
        if (path.name.startswith("test_") or path.name.endswith("_test.py"))
        and re.search(r"""(?:\.accelerator\b|["']accelerator["'])""", path.read_text())
    }
    weighted = {REPOSITORY / filename for filename in json.loads(model.read_text())}
    assert declared | {path for path in weighted if path.is_file()} <= selected


@pytest.mark.parametrize("joined", [False, True])
def test_retry_reports(tmp_path: Path, joined: bool) -> None:
    cleanup = tmp_path / ".github/scripts/delete_gpu_process.sh"
    cleanup.parent.mkdir(parents=True)
    cleanup.write_text("exit 0")
    final = tmp_path / "final reports/result.xml"
    arguments = [f"--junitxml={final}"] if joined else ["--junitxml", str(final)]
    command = 'path="${!#}"; echo "$OMNI_CI_ATTEMPT" > "${path#--junitxml=}"; test "$OMNI_CI_ATTEMPT" = 2'
    subprocess.run(
        [
            "bash",
            str(REPOSITORY / ".github/scripts/run_flaky_pytest.sh"),
            "bash",
            "-c",
            command,
            "pytest",
            *arguments,
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "OMNI_CI_STAGE_LABEL": "shard",
            "RUNNER_TEMP": str(tmp_path),
            "OMNI_CI_RETRY_DELAY_SECONDS": "0",
            "OMNI_CI_MAX_ATTEMPTS": "2",
        },
        check=True,
    )
    reports = tmp_path / "omni-ci-retry"
    assert (reports / "shard-attempt-1.xml").read_text() == "1\n"
    assert (reports / "shard-attempt-2.xml").read_text() == final.read_text() == "2\n"


def test_calibrate_from_runs(tmp_path: Path) -> None:
    existing = tmp_path / "existing.json"
    output = tmp_path / "output.json"
    existing.write_text('{"gone.py": 4, "scripts/ci/utils/pytest_partition.py": 5}')
    classname = "tests.unit_test.ci.test_pytest_partition.TestSuite.Nested"
    for index, seconds in enumerate([2, 4, 6]):
        run = tmp_path / "runs" / str(index)
        run.mkdir(parents=True)
        for shard in range(2):
            (run / f"{shard}.xml").write_text(
                f'<testsuite><testcase classname="{classname}" time="{seconds}"/></testsuite>'
            )
    subprocess.run(
        [
            sys.executable,
            str(REPOSITORY / "scripts/ci/utils/update_pytest_durations.py"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--output",
            str(output),
            "--existing",
            str(existing),
            "--prune-deleted",
        ],
        check=True,
    )
    assert json.loads(output.read_text()) == {
        "scripts/ci/utils/pytest_partition.py": 5,
        Path(__file__).relative_to(REPOSITORY).as_posix(): 11.2,
    }
    unknown = tmp_path / "unknown.xml"
    unknown.write_text(
        '<testsuite><testcase classname="unknown.Module" time="0"/></testsuite>'
    )
    with pytest.raises(ValueError, match="unknown.Module"):
        duration_weights(REPOSITORY, [[unknown]], 90)
