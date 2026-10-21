# SPDX-License-Identifier: Apache-2.0
"""Contract tests for the Cookbook order lint CLI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest

from scripts.cookbook_order_data import DOWNLOADS_DESC, ROADMAP_ORDER

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER_PATH = REPO_ROOT / "scripts" / "check_cookbook_order.py"


def run_checker(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER_PATH), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def cookbook_source() -> str:
    entries = ROADMAP_ORDER + sorted(
        DOWNLOADS_DESC, key=DOWNLOADS_DESC.__getitem__, reverse=True
    )
    return ".. toctree::\n   :maxdepth: 1\n   :caption: Cookbook\n\n" + "".join(
        f"   {entry}\n" for entry in entries
    )


@pytest.mark.parametrize(
    "source_format",
    ["bare", "explicit-title", "caption-first", "four-spaces", "other-sections"],
)
def test_valid_cookbook_passes(
    tmp_path: Path,
    cookbook_source: str,
    source_format: Literal[
        "bare", "explicit-title", "caption-first", "four-spaces", "other-sections"
    ],
) -> None:
    if source_format == "explicit-title":
        cookbook_source = cookbook_source.replace(
            "cookbook/qwen3_tts.md", "Qwen <cookbook/qwen3_tts.md>"
        )
    elif source_format == "caption-first":
        cookbook_source = cookbook_source.replace(
            "   :maxdepth: 1\n   :caption: Cookbook",
            "   :caption: Cookbook\n   :maxdepth: 1",
        )
    elif source_format == "four-spaces":
        cookbook_source = cookbook_source.replace("   ", "    ")
    elif source_format == "other-sections":
        cookbook_source = (
            ".. toctree::\n   :caption: Guides\n\n   guide.md\n\n"
            + cookbook_source
            + "\nNext section\n============\n\n"
            "   This indented paragraph is outside the Cookbook.\n\n"
            ".. toctree::\n   :caption: Reference\n\n   reference.md\n"
        )
    else:
        pass
    cookbook_path = tmp_path / "index.rst"
    cookbook_path.write_text(cookbook_source, encoding="utf-8")

    result = run_checker(str(cookbook_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK:" in result.stdout


@pytest.mark.parametrize(
    ("extra_entry", "diagnostic"),
    [
        ("Qwen <cookbook/qwen3_tts.md>", "duplicate entry 'cookbook/qwen3_tts.md'"),
        ("New model <cookbook/new_model.md>", "unknown entry 'cookbook/new_model.md'"),
        ("cookbook/UNKNOWN.md", "unknown entry 'cookbook/UNKNOWN.md'"),
    ],
)
def test_extra_entries_are_checked(
    tmp_path: Path, cookbook_source: str, extra_entry: str, diagnostic: str
) -> None:
    cookbook_path = tmp_path / "index.rst"
    cookbook_path.write_text(cookbook_source + f"   {extra_entry}\n", encoding="utf-8")

    result = run_checker(str(cookbook_path))

    assert result.returncode == 1, result.stdout + result.stderr
    assert diagnostic in result.stdout
    assert "OK:" not in result.stdout


@pytest.mark.parametrize("include_valid_target", [False, True])
def test_missing_target_fails(
    tmp_path: Path, cookbook_source: str, include_valid_target: bool
) -> None:
    missing_path = tmp_path / "missing.rst"
    target_paths = [str(missing_path)]
    if include_valid_target:
        cookbook_path = tmp_path / "index.rst"
        cookbook_path.write_text(cookbook_source, encoding="utf-8")
        target_paths.append(str(cookbook_path))
    else:
        pass

    result = run_checker(*target_paths)

    assert result.returncode == 1, result.stdout + result.stderr
    assert str(missing_path) in result.stdout + result.stderr
    assert "OK:" not in result.stdout


def test_download_order_diagnostic_gives_correct_direction(
    tmp_path: Path, cookbook_source: str
) -> None:
    cookbook_path = tmp_path / "index.rst"
    cookbook_path.write_text(
        cookbook_source.replace(
            "   cookbook/qwen3_asr.md\n   cookbook/fun_asr.md\n",
            "   cookbook/fun_asr.md\n   cookbook/qwen3_asr.md\n",
        ),
        encoding="utf-8",
    )

    result = run_checker(str(cookbook_path))

    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        "'cookbook/fun_asr.md' "
        f"({DOWNLOADS_DESC['cookbook/fun_asr.md']:,}) must come after "
        "'cookbook/qwen3_asr.md' "
        f"({DOWNLOADS_DESC['cookbook/qwen3_asr.md']:,})"
    ) in result.stdout
