# SPDX-License-Identifier: Apache-2.0
"""Collect changed test cases and verify contract-review evidence."""

from __future__ import annotations

import ast
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath

from scripts.docs_review.models import (
    CollectedFile,
    Finding,
    ReviewBundle,
    SourceCitation,
    UnitTestCase,
)
from scripts.docs_review.snapshot import (
    citation_matches,
    clip_content,
    git_show,
    require_git,
)

UNIT_TEST_POLICY_VERSION = "unit-tests-1"
UNIT_TEST_ROOT = "tests/unit_test/"
AUTOMATIC_CONTEXT_BYTES = 36_000
CONTRACT_MODULES = {"sglang_omni", "sglang_omni_router", "sglang_omni_mlx", "examples"}
CONTEXT_SUFFIXES = {
    ".py",
    ".md",
    ".rst",
    ".rs",
    ".cpp",
    ".h",
    ".toml",
    ".json",
    ".yaml",
    ".yml",
}
DUPLICATE_RULE_ID = "UNIT_TEST_DUPLICATE"
UNIT_TEST_RULE_IDS = {
    "UNIT_TEST_INTERNAL_ASSERTION",
    "UNIT_TEST_SELF_DERIVED_EXPECTATION",
    DUPLICATE_RULE_ID,
}
INSUFFICIENT_EVIDENCE = "UNIT_TEST_INSUFFICIENT_EVIDENCE"
TestFunction = ast.FunctionDef | ast.AsyncFunctionDef


def is_unit_test_file(file_path: str) -> bool:
    name = PurePosixPath(file_path).name
    return (
        file_path.startswith(UNIT_TEST_ROOT)
        and name.endswith(".py")
        and (name.startswith("test_") or name.endswith("_test.py"))
    )


def is_context_path(file_path: str) -> bool:
    path = PurePosixPath(file_path)
    return bool(
        path.parts
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == file_path
        and path.suffix in CONTEXT_SUFFIXES
        and (
            path.parts[0] in CONTRACT_MODULES | {"docs"}
            or file_path.startswith(UNIT_TEST_ROOT)
        )
    )


def changed_test_paths(
    repository: Path, merge_base: str, head_commit_sha: str
) -> list[tuple[str | None, str]]:
    diff = require_git(
        repository,
        [
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--diff-filter=AMRT",
            merge_base,
            head_commit_sha,
            "--",
            UNIT_TEST_ROOT,
        ],
    )
    entries = iter(diff.rstrip("\0").split("\0") if diff else [])
    paths: list[tuple[str | None, str]] = []
    for status in entries:
        previous_path = next(entries)
        if status.startswith("R"):
            head_path = next(entries)
        else:
            head_path = previous_path
        if head_path.endswith(".py"):
            paths.append((None if status == "A" else previous_path, head_path))
        else:
            pass
    return paths


def test_definitions(
    suite: ast.Module | ast.ClassDef, prefix: str = ""
) -> dict[str, TestFunction]:
    definitions: dict[str, TestFunction] = {}
    for statement in suite.body:
        if isinstance(statement, ast.ClassDef) and statement.name.startswith("Test"):
            definitions.update(
                test_definitions(statement, f"{prefix}{statement.name}.")
            )
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            statement.name.startswith("test_")
        ):
            definitions[f"{prefix}{statement.name}"] = statement
        else:
            pass
    return definitions


def case_start(definition: TestFunction) -> int:
    return min(
        [
            definition.lineno,
            *(decorator.lineno for decorator in definition.decorator_list),
        ]
    )


def changed_case_ranges(
    before: TestFunction | None,
    after: TestFunction,
    before_lines: list[str],
    after_lines: list[str],
) -> list[tuple[int, int]]:
    after_start = case_start(after)
    assert after.end_lineno is not None
    if before is None:
        return [(after_start, after.end_lineno)]
    else:
        assert before.end_lineno is not None
        before_case = before_lines[case_start(before) - 1 : before.end_lineno]
        after_case = after_lines[after_start - 1 : after.end_lineno]
        changes = SequenceMatcher(None, before_case, after_case, autojunk=False)
        # A deletion has no added line, so cite the nearest surviving line in the case.
        return [
            (
                after_start + min(head_start, len(after_case) - 1),
                after_start + max(min(head_start, len(after_case) - 1), head_end - 1),
            )
            for tag, _, _, head_start, head_end in changes.get_opcodes()
            if tag != "equal"
        ]


def referenced_contract_modules(
    test_module: ast.Module, changed_names: set[str]
) -> list[str]:
    modules: list[str] = []
    for imported in ast.walk(test_module):
        if isinstance(imported, ast.ImportFrom) and imported.level == 0:
            if imported.module is not None and any(
                (alias.asname or alias.name) in changed_names
                for alias in imported.names
            ):
                modules.append(imported.module)
            else:
                pass
        elif isinstance(imported, ast.Import):
            modules.extend(
                alias.name
                for alias in imported.names
                if (alias.asname or alias.name.split(".")[0]) in changed_names
            )
        else:
            pass
    return [
        module
        for module in dict.fromkeys(modules)
        if module.split(".")[0] in CONTRACT_MODULES
    ]


def collect_unit_test_inputs(
    repository: Path, merge_base: str, head_commit_sha: str, include_paths: list[str]
) -> tuple[list[CollectedFile], list[UnitTestCase], list[str]]:
    files: dict[str, CollectedFile] = {}
    cases: list[UnitTestCase] = []
    limitations: list[str] = []
    contract_modules: list[str] = []
    for previous_path, head_path in changed_test_paths(
        repository, merge_base, head_commit_sha
    ):
        head_source = git_show(repository, head_commit_sha, head_path)
        before_source = (
            git_show(repository, merge_base, previous_path)
            if previous_path is not None
            else ""
        )
        if head_source is None or before_source is None:
            limitations.append(f"{head_path}: test revision could not be read")
            continue
        else:
            pass
        content, truncated = clip_content(head_source)
        if truncated:
            limitations.append(f"{head_path}: test source exceeds the collection limit")
            continue
        else:
            pass
        try:
            before_module = ast.parse(before_source)
            before_definitions = test_definitions(before_module)
            head_module = ast.parse(head_source)
        except (SyntaxError, ValueError):
            limitations.append(f"{head_path}: test source could not be parsed")
            continue
        else:
            pass
        if not is_unit_test_file(head_path):
            if ast.dump(before_module) != ast.dump(head_module):
                limitations.append(
                    f"{head_path}: shared fixture or helper change is outside direct test review"
                )
            else:
                pass
            continue
        else:
            pass
        referenced_names: set[str] = set()
        before_lines = before_source.splitlines()
        head_lines = head_source.splitlines()
        has_changed_case = False
        for name, definition in test_definitions(head_module).items():
            previous = before_definitions.get(name)
            if previous is not None and ast.dump(previous) == ast.dump(definition):
                continue
            else:
                pass
            assert definition.end_lineno is not None
            cases.append(
                UnitTestCase(
                    file_path=head_path,
                    name=name,
                    line_start=case_start(definition),
                    line_end=definition.end_lineno,
                    previous_source=(
                        "\n".join(
                            before_lines[case_start(previous) - 1 : previous.end_lineno]
                        )
                        if previous is not None
                        else ""
                    ),
                    changed_line_ranges=changed_case_ranges(
                        previous, definition, before_lines, head_lines
                    ),
                )
            )
            referenced_names.update(
                node.id for node in ast.walk(definition) if isinstance(node, ast.Name)
            )
            has_changed_case = True
        if has_changed_case:
            files[head_path] = CollectedFile(
                file_path=head_path,
                content=content,
                truncated=False,
                change="added" if previous_path is None else "modified",
            )
            contract_modules.extend(
                referenced_contract_modules(head_module, referenced_names)
            )
        elif ast.dump(before_module) != ast.dump(head_module):
            limitations.append(
                f"{head_path}: test module changed outside the directly reviewed cases"
            )
        else:
            pass

    for file_path in dict.fromkeys(include_paths):
        if file_path in files:
            continue
        elif not is_context_path(file_path):
            limitations.append(f"{file_path}: unsupported evidence path")
            continue
        else:
            pass
        evidence = git_show(repository, head_commit_sha, file_path)
        if evidence is None:
            limitations.append(f"{file_path}: evidence was not found at head")
        else:
            content, truncated = clip_content(evidence)
            files[file_path] = CollectedFile(
                file_path=file_path,
                content=content,
                truncated=truncated,
                change="included",
            )
    remaining_context_bytes = AUTOMATIC_CONTEXT_BYTES
    for module in dict.fromkeys(contract_modules):
        module_path = module.replace(".", "/")
        for file_path in (f"{module_path}.py", f"{module_path}/__init__.py"):
            if file_path in files:
                break
            else:
                pass
            evidence = git_show(repository, head_commit_sha, file_path)
            if evidence is None:
                continue
            else:
                evidence_bytes = len(evidence.encode())
            if evidence_bytes <= remaining_context_bytes:
                files[file_path] = CollectedFile(
                    file_path=file_path,
                    content=evidence,
                    truncated=False,
                    change="included",
                )
                remaining_context_bytes -= evidence_bytes
            else:
                pass
            break
    if cases and not any(
        is_context_path(file_path) and not file_path.startswith(UNIT_TEST_ROOT)
        for file_path in files
    ):
        limitations.append(
            "No contract source was collected. Include the relevant source or docs."
        )
    else:
        pass
    return list(files.values()), cases, limitations


def covers_other_test(
    bundle: ReviewBundle, case: UnitTestCase, duplicate: SourceCitation
) -> bool:
    if (
        not is_unit_test_file(duplicate.file_path)
        or not duplicate.quote.strip()
        or not citation_matches(
            bundle,
            duplicate.file_path,
            duplicate.line_start,
            duplicate.line_end,
            duplicate.quote,
        )
        or (
            duplicate.file_path == case.file_path
            and duplicate.line_start <= case.line_end
            and case.line_start <= duplicate.line_end
        )
    ):
        return False
    else:
        pass
    source = next(
        file.content for file in bundle.files if file.file_path == duplicate.file_path
    )
    try:
        definitions = test_definitions(ast.parse(source))
    except (SyntaxError, ValueError):
        return False
    else:
        return any(
            definition.end_lineno is not None
            and case_start(definition)
            <= duplicate.line_start
            <= duplicate.line_end
            <= definition.end_lineno
            for definition in definitions.values()
        )


def validate_unit_test_findings(
    findings: list[Finding], bundle: ReviewBundle
) -> tuple[list[Finding], list[Finding], int, list[str]]:
    accepted: list[Finding] = []
    pre_existing: list[Finding] = []
    limitations: list[str] = []
    dropped = 0
    contract_paths = {
        file.file_path
        for file in bundle.files
        if is_context_path(file.file_path)
        and not file.file_path.startswith(UNIT_TEST_ROOT)
    }
    for finding in findings:
        case = next(
            (
                case
                for case in bundle.unit_test_cases
                if case.file_path == finding.file_path
                and case.line_start
                <= finding.line_start
                <= finding.line_end
                <= case.line_end
                and any(
                    start <= finding.line_end and finding.line_start <= end
                    for start, end in case.changed_line_ranges
                )
            ),
            None,
        )
        if (
            case is None
            or not finding.quote.strip()
            or not citation_matches(
                bundle,
                finding.file_path,
                finding.line_start,
                finding.line_end,
                finding.quote,
            )
        ):
            dropped += 1
        elif (
            finding.rule_id == INSUFFICIENT_EVIDENCE
            and finding.certainty == "uncertain"
        ):
            limitations.append(
                f"{case.file_path}:{case.line_start}: insufficient contract evidence "
                f"for {case.name}. {finding.claim.strip()}"
            )
        elif (
            finding.rule_id not in UNIT_TEST_RULE_IDS
            or finding.certainty != "supported"
            or not finding.claim.strip()
            or not finding.public_contract.strip()
            or not finding.regression.strip()
            or not finding.impact.strip()
            or not finding.suggested_fix.strip()
            or not finding.evidence_quote.strip()
            or finding.evidence_file_path not in contract_paths
            or not citation_matches(
                bundle,
                finding.evidence_file_path,
                finding.evidence_line_start,
                finding.evidence_line_end,
                finding.evidence_quote,
            )
        ):
            dropped += 1
        elif finding.rule_id == DUPLICATE_RULE_ID and (
            finding.duplicate_test is None
            or not covers_other_test(bundle, case, finding.duplicate_test)
        ):
            dropped += 1
        elif (
            finding.rule_id != DUPLICATE_RULE_ID and finding.duplicate_test is not None
        ):
            dropped += 1
        elif finding.change_relation == "pre_existing":
            pre_existing.append(finding)
        else:
            accepted.append(finding)
    if dropped:
        limitations.append(
            f"{dropped} unit-test findings lacked valid scope, evidence, or regression details"
        )
    else:
        pass
    return accepted, pre_existing, dropped, limitations
