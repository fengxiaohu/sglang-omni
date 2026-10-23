# SPDX-License-Identifier: Apache-2.0
"""Exercise the documentation emoji checker with synthetic Markdown inputs."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import emoji
import pytest

from scripts.check_doc_style import check_docs, check_source

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/check_doc_style.py"
EMOJI_VERSION_17_SCALAR = next(
    sequence
    for sequence, properties in emoji.EMOJI_DATA.items()
    if len(sequence) == 1 and properties.get("E") == 17
)


def detail_has_complete_sequence(detail: str, sequence: str) -> bool:
    """Match either literal or repr-formatted complete diagnostic sequences."""
    return detail.endswith(sequence) or detail.endswith(repr(sequence))


@pytest.mark.parametrize(
    "symbol",
    ["©", "®", "™", "☀", "♟", "↔", "0", "9"],
)
def test_check_source_allows_text_symbols_and_bare_ascii_symbols(symbol: str) -> None:
    violations = check_source(Path("guide.md"), f"Allowed {symbol}.\n")

    assert violations == []


@pytest.mark.parametrize("sequence", ["©\ufe0e", "↔\ufe0e", "⌚\ufe0e"])
def test_check_source_allows_legal_text_presentation_selectors(sequence: str) -> None:
    violations = check_source(Path("guide.md"), f"Text presentation: {sequence}\n")

    assert violations == []


@pytest.mark.parametrize("sequence", ["#\ufe0e", "*\ufe0e"])
def test_check_source_allows_ascii_text_presentation_selectors(sequence: str) -> None:
    violations = check_source(Path("guide.md"), f"Text presentation: {sequence}\n")

    assert violations == []


@pytest.mark.parametrize("sequence", ["©\ufe0f", "☀\ufe0f", "⌚\ufe0f"])
def test_check_source_rejects_emoji_presentation_selectors(sequence: str) -> None:
    violations = check_source(Path("guide.md"), f"Emoji presentation: {sequence}\n")

    assert len(violations) == 1
    assert violations[0].rule == "no-emoji"
    assert violations[0].line_number == 1
    assert detail_has_complete_sequence(violations[0].detail, sequence)


def test_check_source_rejects_default_emoji_presentation_without_text_selector() -> (
    None
):
    violations = check_source(Path("guide.md"), "A watch ⌚ is an emoji.\n")

    assert len(violations) == 1
    assert violations[0].rule == "no-emoji"


def test_check_source_does_not_treat_an_invalid_text_selector_as_an_exemption() -> None:
    violations = check_source(Path("guide.md"), "Invalid text selector 😀\ufe0e.\n")

    assert len(violations) == 1
    assert violations[0].rule == "no-emoji"


@pytest.mark.parametrize(
    "sequence",
    [
        "🏽",
        "1\u20e3",
        "#\ufe0f\u20e3",
        "#\u20e3",
        "*\ufe0f\u20e3",
        "*\u20e3",
        "👍🏽",
        "👩‍💻",
        "👩🏻‍💻",
        "🏳‍🌈",
        "👁‍🗨",
        "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
        EMOJI_VERSION_17_SCALAR,
    ],
)
def test_check_source_reports_one_violation_for_each_complete_emoji_sequence(
    sequence: str,
) -> None:
    violations = check_source(Path("guide.md"), f"Sequence: {sequence}\n")

    assert len(violations) == 1
    assert violations[0].rule == "no-emoji"
    assert violations[0].line_number == 1
    assert detail_has_complete_sequence(violations[0].detail, sequence)


def test_check_source_reports_non_rgi_emoji_bases_separately() -> None:
    violations = check_source(Path("guide.md"), "Non-RGI 😀‍😀 sequence.\n")

    assert len(violations) == 2
    assert all(violation.rule == "no-emoji" for violation in violations)
    assert all(violation.line_number == 1 for violation in violations)
    assert all("😀" in violation.detail for violation in violations)


@pytest.mark.parametrize(
    "sequence, expected_count, expected_line_numbers",
    [
        pytest.param(
            "\U0001f1e8",
            1,
            [1],
            id="lone-ri-U+1F1E8",
        ),
        pytest.param(
            "\U0001f1e8\U0001f1f3",
            1,
            [1],
            id="valid-flag-U+1F1E8-U+1F1F3",
        ),
        pytest.param(
            "\U0001f1ff\U0001f1ff",
            2,
            [1, 1],
            id="invalid-ri-pair-U+1F1FF-U+1F1FF",
        ),
        pytest.param("1", 0, [], id="bare-digit-U+0031"),
        pytest.param("#", 0, [], id="bare-hash-U+0023"),
        pytest.param("*", 0, [], id="bare-asterisk-U+002A"),
        pytest.param("1\ufe0f", 1, [1], id="digit-vs16-U+0031-U+FE0F"),
        pytest.param("#\ufe0f", 1, [1], id="hash-vs16-U+0023-U+FE0F"),
        pytest.param("*\ufe0f", 1, [1], id="asterisk-vs16-U+002A-U+FE0F"),
        pytest.param(
            "1\ufe0f\u20e3",
            1,
            [1],
            id="keycap-digit-U+0031-U+FE0F-U+20E3",
        ),
        pytest.param("1\ufe0e", 0, [], id="digit-vs15-U+0031-U+FE0E"),
        pytest.param("A\ufe0f", 0, [], id="letter-vs16-U+0041-U+FE0F"),
    ],
)
def test_check_source_matches_the_supplement_codepoint_table(
    sequence: str, expected_count: int, expected_line_numbers: list[int]
) -> None:
    violations = check_source(Path("guide.md"), f"Codepoint {sequence}.\n")

    assert len(violations) == expected_count
    assert [violation.line_number for violation in violations] == expected_line_numbers
    assert all(violation.rule == "no-emoji" for violation in violations)


def test_check_source_does_not_double_count_valid_sequences_near_supplements() -> None:
    source = "\U0001f1e8\U0001f1f3\U0001f1ff\U0001f1ff"
    source += "1\ufe0f\u20e31\u20e31\ufe0f\n"

    violations = check_source(Path("guide.md"), source)

    assert len(violations) == 6
    assert (
        sum(
            detail_has_complete_sequence(violation.detail, "\U0001f1e8\U0001f1f3")
            for violation in violations
        )
        == 1
    )
    assert (
        sum(
            detail_has_complete_sequence(violation.detail, "1\ufe0f\u20e3")
            for violation in violations
        )
        == 1
    )
    assert (
        sum(
            detail_has_complete_sequence(violation.detail, "1\u20e3")
            for violation in violations
        )
        == 1
    )
    assert (
        sum(
            detail_has_complete_sequence(violation.detail, "\U0001f1ff")
            for violation in violations
        )
        == 2
    )
    assert (
        sum(
            detail_has_complete_sequence(violation.detail, "1\ufe0f")
            for violation in violations
        )
        == 1
    )


def test_check_source_applies_supplements_outside_fences_and_inside_inline_code() -> (
    None
):
    source = (
        "~~~text\n\U0001f1e8 1\ufe0f 1\ufe0f\u20e3\n~~~\n"
        "Inline `\U0001f1e8 1\ufe0f` and 1\ufe0f.\n"
    )

    violations = check_source(Path("guide.md"), source)

    assert len(violations) == 3
    assert all(violation.line_number == 4 for violation in violations)
    assert sum("\U0001f1e8" in violation.detail for violation in violations) == 1
    assert sum("1\ufe0f" in violation.detail for violation in violations) == 2


def test_check_source_reports_repeated_sequences_once_each() -> None:
    source = "😀😀 \U0001f1e8\U0001f1f3\U0001f1e8\U0001f1fa\n"
    violations = check_source(Path("guide.md"), source)

    assert len(violations) == 4
    assert all(violation.rule == "no-emoji" for violation in violations)
    assert all(violation.line_number == 1 for violation in violations)


def test_check_source_reports_source_line_numbers() -> None:
    source = "A clean line.\n\n😀 on line three.\nA clean line.\n👍🏽 on line five.\n"

    violations = check_source(Path("guide.md"), source)

    assert [violation.line_number for violation in violations] == [3, 5]


def test_check_source_ignores_emoji_inside_matching_fences() -> None:
    source = (
        "  ````python\n"
        "😀 inside a longer backtick fence\n"
        "   ```\n"
        "😀 a shorter marker does not close it\n"
        "  ```` \t\n"
        "😀 outside the backtick fence\n"
        "   ~~~text\n"
        "😀 inside a tilde fence\n"
        "  ```\n"
        "😀 a different marker does not close it\n"
        "  ~~~\t\n"
        "😀 outside the tilde fence\n"
    )

    violations = check_source(Path("guide.md"), source)

    assert [violation.line_number for violation in violations] == [6, 12]


def test_check_source_requires_closing_fence_trailing_text_to_be_absent() -> None:
    source = (
        "```python\n😀 inside\n``` trailing text\n😀 still inside\n```\n😀 outside\n"
    )

    violations = check_source(Path("guide.md"), source)

    assert [violation.line_number for violation in violations] == [6]


def test_check_source_checks_inline_code_and_urls() -> None:
    source = (
        "Inline `😀` code.\nhttps://example.test/😀\n[link](https://example.test/😀)\n"
    )

    violations = check_source(Path("guide.md"), source)

    assert len(violations) == 3
    assert all(violation.rule == "no-emoji" for violation in violations)
    assert [violation.line_number for violation in violations] == [1, 2, 3]


def test_check_docs_scans_all_markdown_files_recursively(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    nested_root = docs_root / "nested" / "deeper"
    nested_root.mkdir(parents=True)
    (docs_root / "clean.md").write_text("# Clean\n", encoding="utf-8")
    nested_path = nested_root / "guide.md"
    nested_path.write_text("# Guide\n\nNested 😀.\n", encoding="utf-8")
    (docs_root / "ignored.txt").write_text("😀\n", encoding="utf-8")

    violations = check_docs(docs_root)

    assert len(violations) == 1
    assert violations[0].path == nested_path
    assert violations[0].line_number == 3
    assert violations[0].rule == "no-emoji"


def copy_script_fixture(tmp_path: Path, document_text: str) -> tuple[Path, Path]:
    """Create an isolated scripts/docs tree for CLI checks."""
    repository_root = tmp_path / "fixture"
    script_path = repository_root / "scripts" / "check_doc_style.py"
    document_path = repository_root / "docs" / "guide.md"
    script_path.parent.mkdir(parents=True)
    document_path.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script_path)
    document_path.write_text(document_text, encoding="utf-8")
    return repository_root, document_path


@pytest.mark.parametrize(
    "document_text, expected_exit_code",
    [("# Clean\n", 0), ("# Bad\n\n😀\n", 1)],
)
def test_cli_scans_the_copied_script_fixture(
    tmp_path: Path, document_text: str, expected_exit_code: int
) -> None:
    repository_root, document_path = copy_script_fixture(tmp_path, document_text)
    script_path = repository_root / "scripts" / "check_doc_style.py"

    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=repository_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == expected_exit_code
    if expected_exit_code == 0:
        assert result.stderr == ""
    else:
        assert f"docs/{document_path.name}:3: no-emoji:" in result.stderr


def test_cli_rejects_arguments_with_exit_status_two(tmp_path: Path) -> None:
    repository_root, _ = copy_script_fixture(tmp_path, "# Clean\n")
    script_path = repository_root / "scripts" / "check_doc_style.py"

    result = subprocess.run(
        [sys.executable, str(script_path), "--unexpected"],
        cwd=repository_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert "unrecognized arguments" in result.stderr
