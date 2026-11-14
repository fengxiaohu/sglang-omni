# SPDX-License-Identifier: Apache-2.0
"""Check for coupled expectations in tests of config resolve stage overrides."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from textwrap import dedent
from typing import Literal

from scripts.docs_review.models import Finding, ReviewBundle, SourceCitation
from scripts.docs_review.unit_tests import TestFunction, test_definitions

CONFIG_CLI_PATH = "sglang_omni/cli/config.py"
CONFIG_MANAGER_MODULE = "sglang_omni.config.manager"
REVIEWED_IMPORTS = {"yaml", "config_app", "ConfigManager", "dump_user_config"}
SELF_DERIVED_EXPECTATION = "UNIT_TEST_SELF_DERIVED_EXPECTATION"


@dataclass(frozen=True, kw_only=True)
class StageOverrideExpectation:
    comparison: ast.Assert
    stage_name_variable: str
    memory_fraction: float
    has_independent_assertion: bool


def assigned_value(expression: ast.expr, assignments: dict[str, ast.expr]) -> ast.expr:
    while isinstance(expression, ast.Name):
        assigned = assignments.get(expression.id)
        if assigned is None or assigned.lineno >= expression.lineno:
            break
        else:
            expression = assigned
    return expression


def call_with_arguments(expression: ast.expr, argument_count: int) -> ast.Call | None:
    if (
        isinstance(expression, ast.Call)
        and not expression.keywords
        and len(expression.args) == argument_count
    ):
        return expression
    else:
        return None


def shared_stage_override(definition: TestFunction) -> StageOverrideExpectation | None:
    assignments: dict[str, ast.expr] = {}
    output_assertions: list[ast.Assert] = []
    for statement in definition.body:
        match statement:
            case ast.Assign(targets=[ast.Name(id=name)], value=value) if (
                name not in assignments and name not in REVIEWED_IMPORTS
            ):
                assignments[name] = value
            case ast.Expr(value=ast.Constant(value=str())):
                pass
            case ast.Assert(
                test=ast.Compare(
                    left=ast.Attribute(value=ast.Name(), attr="exit_code"),
                    ops=[ast.Eq()],
                    comparators=[ast.Constant(value=0)],
                )
            ):
                pass
            case ast.Assert():
                output_assertions.append(statement)
            case _:
                return None

    if not 1 <= len(output_assertions) <= 2:
        return None
    else:
        comparison = output_assertions[0]
    match comparison.test:
        case ast.Compare(left=observed, ops=[ast.Eq()], comparators=[expected]):
            output = call_with_arguments(assigned_value(observed, assignments), 1)
            expected_dump = call_with_arguments(
                assigned_value(expected, assignments), 1
            )
        case _:
            return None
    if (
        output is None
        or expected_dump is None
        or ast.unparse(output.func) != "yaml.safe_load"
        or ast.unparse(expected_dump.func) != "dump_user_config"
        or not isinstance(output.args[0], ast.Attribute)
        or output.args[0].attr != "stdout"
    ):
        return None
    else:
        invocation = call_with_arguments(
            assigned_value(output.args[0].value, assignments), 2
        )
        merge = call_with_arguments(
            assigned_value(expected_dump.args[0], assignments), 1
        )
    if (
        invocation is None
        or merge is None
        or not isinstance(invocation.func, ast.Attribute)
        or invocation.func.attr != "invoke"
        or not isinstance(merge.func, ast.Attribute)
        or merge.func.attr != "merge_config"
        or ast.unparse(invocation.args[0]) != "config_app"
    ):
        return None
    else:
        parser = call_with_arguments(assigned_value(merge.args[0], assignments), 1)
        manager = call_with_arguments(assigned_value(merge.func.value, assignments), 1)
    match invocation.args[1]:
        case ast.List(
            elts=[
                ast.Constant(value="resolve"),
                ast.Constant(value="--config"),
                config_argument,
                ast.Starred(value=cli_arguments),
            ]
        ):
            override = assigned_value(cli_arguments, assignments)
        case _:
            return None
    if (
        parser is None
        or manager is None
        or not isinstance(parser.func, ast.Attribute)
        or parser.func.attr != "parse_extra_args"
        or ast.unparse(manager.func) != "ConfigManager.from_file"
        or ast.unparse(manager.args[0]) != ast.unparse(config_argument)
        or assigned_value(parser.func.value, assignments) is not manager
        or assigned_value(parser.args[0], assignments) is not override
    ):
        return None
    else:
        pass

    match override:
        case ast.List(
            elts=[
                ast.JoinedStr(
                    values=[
                        ast.Constant(value="--"),
                        ast.FormattedValue(
                            value=ast.Name(id=stage_name_variable),
                            conversion=-1,
                            format_spec=None,
                        ),
                        ast.Constant(value=".engine.mem_fraction_static"),
                    ]
                ),
                ast.Constant(value=str() as fraction_text),
            ]
        ):
            try:
                fraction = float(fraction_text)
            except ValueError:
                return None
            else:
                pass
        case _:
            return None
    if not 0 < fraction < 1:
        return None
    else:
        pass

    if len(output_assertions) == 2:
        match output_assertions[1].test:
            case ast.Compare(
                left=actual,
                ops=[ast.Eq()],
                comparators=[ast.Constant(value=checked_fraction)],
            ) if (
                ast.unparse(actual)
                == f"{ast.unparse(observed)}['stages'][{stage_name_variable}]['engine']['mem_fraction_static']"
                and checked_fraction == fraction
            ):
                pass
            case _:
                return None
    else:
        pass
    return StageOverrideExpectation(
        comparison=comparison,
        stage_name_variable=stage_name_variable,
        memory_fraction=fraction,
        has_independent_assertion=len(output_assertions) == 2,
    )


def config_resolve_citation(source: str) -> SourceCitation | None:
    try:
        module = ast.parse(source)
    except (ValueError, SyntaxError):
        return None
    else:
        definitions = {
            statement.name: statement
            for statement in module.body
            if isinstance(statement, ast.FunctionDef)
        }
    command = definitions.get("resolve")
    resolver = definitions.get("resolve_sources")
    if command is None or resolver is None:
        return None
    else:
        pass
    entry_point = ast.get_docstring(command)
    if entry_point is None or "including dotted overrides" not in entry_point:
        return None
    else:
        pass
    registers_command = any(
        isinstance(decorator, ast.Call)
        and ast.unparse(decorator.func) == "config_app.command"
        for decorator in command.decorator_list
    )
    has_shared_parser = (
        any(
            isinstance(statement, ast.ImportFrom)
            and statement.level == 0
            and statement.module == CONFIG_MANAGER_MODULE
            and any(
                imported.name == "ConfigManager" and imported.asname is None
                for imported in statement.names
            )
            for statement in module.body
        )
        and any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "resolve_sources"
            and any(
                keyword.arg == "argv" and ast.unparse(keyword.value) == "ctx.args"
                for keyword in node.keywords
            )
            for node in ast.walk(command)
        )
        and any(
            isinstance(node, ast.Call)
            and ast.unparse(node)
            == "ConfigManager(baseline).parse_extra_args(list(argv))"
            for node in ast.walk(resolver)
        )
    )
    if not (registers_command and has_shared_parser):
        return None
    else:
        pass
    docstring = command.body[0]
    start = docstring.lineno
    lines = source.splitlines()
    end = next(
        (
            line_number
            for line_number in range(start, (docstring.end_lineno or start) + 1)
            if "including dotted overrides" in lines[line_number - 1]
        ),
        docstring.end_lineno or start,
    )
    return SourceCitation(
        file_path=CONFIG_CLI_PATH,
        line_start=start,
        line_end=end,
        quote="\n".join(lines[start - 1 : end]),
    )


def audit_config_resolve(
    bundle: ReviewBundle,
) -> tuple[dict[tuple[str, str], Finding], set[tuple[str, str]]]:
    sources = {source.file_path: source.content for source in bundle.files}
    citation = config_resolve_citation(sources.get(CONFIG_CLI_PATH, ""))
    if citation is None:
        return {}, set()
    else:
        pass
    findings: dict[tuple[str, str], Finding] = {}
    independently_checked: set[tuple[str, str]] = set()
    for path in dict.fromkeys(case.file_path for case in bundle.unit_test_cases):
        source = sources[path]
        module = ast.parse(source)
        lines = source.splitlines()
        imports = {
            (statement.module, imported.name)
            for statement in module.body
            if isinstance(statement, ast.ImportFrom) and statement.level == 0
            for imported in statement.names
            if imported.asname is None
        }
        if not {
            ("sglang_omni.cli.config", "config_app"),
            (CONFIG_MANAGER_MODULE, "ConfigManager"),
            ("sglang_omni.config.sources", "dump_user_config"),
        }.issubset(imports) or not any(
            isinstance(statement, ast.Import)
            and any(
                imported.name == "yaml" and imported.asname is None
                for imported in statement.names
            )
            for statement in module.body
        ):
            continue
        else:
            pass
        definitions = test_definitions(module)
        for case in bundle.unit_test_cases:
            if case.file_path != path:
                continue
            else:
                pass
            override = shared_stage_override(definitions[case.name])
            if override is None:
                continue
            elif override.has_independent_assertion:
                independently_checked.add((case.file_path, case.name))
                continue
            else:
                assertion = override.comparison
            end = assertion.end_lineno or assertion.lineno
            start = max(
                (
                    min(last, assertion.lineno)
                    for first, last in case.changed_line_ranges
                    if first <= end
                ),
                default=0,
            )
            if not start:
                continue
            else:
                pass
            try:
                previous_statements = ast.parse(dedent(case.previous_source)).body
            except (SyntaxError, ValueError):
                previous_statements = []
            else:
                pass
            previous = (
                shared_stage_override(previous_statements[0])
                if previous_statements
                and isinstance(
                    previous_statements[0], (ast.FunctionDef, ast.AsyncFunctionDef)
                )
                else None
            )
            stage_name_variable = override.stage_name_variable
            fraction = override.memory_fraction
            change_relation: Literal[
                "introduced", "worsened", "pre_existing", "undetermined"
            ]
            if previous is not None:
                change_relation = (
                    "worsened" if previous.has_independent_assertion else "pre_existing"
                )
            else:
                change_relation = (
                    "undetermined" if case.previous_source else "introduced"
                )
            findings[(case.file_path, case.name)] = Finding(
                rule_id=SELF_DERIVED_EXPECTATION,
                file_path=path,
                line_start=start,
                line_end=end,
                quote="\n".join(lines[start - 1 : end]),
                claim="The CLI and the expected YAML use the same parser for the override.",
                evidence_file_path=citation.file_path,
                evidence_line_start=citation.line_start,
                evidence_line_end=citation.line_end,
                evidence_quote=citation.quote,
                public_contract=(
                    f"With --<{stage_name_variable}>.engine.mem_fraction_static {fraction}, config resolve "
                    f"writes {fraction} for the named stage's memory fraction to YAML on stdout."
                ),
                regression=(
                    f"If the shared parser ignores or changes the requested {fraction}, "
                    "both values can agree while stdout contains the default or a different value."
                ),
                impact="The test still passes when callers receive the wrong memory fraction.",
                suggested_fix=(
                    "Parse stdout and assert "
                    f'configuration["stages"][{stage_name_variable}]["engine"]["mem_fraction_static"] == {fraction}.'
                ),
                change_relation=change_relation,
                certainty="supported",
            )
    return findings, independently_checked
