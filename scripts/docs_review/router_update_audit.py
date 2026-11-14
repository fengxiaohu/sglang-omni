# SPDX-License-Identifier: Apache-2.0
"""Find helper-only tests of interrupted router weight updates."""

from __future__ import annotations

import ast

from scripts.docs_review.models import Finding, ReviewBundle
from scripts.docs_review.unit_tests import test_definitions

ROUTER_APP_PATH = "sglang_omni_router/python/app.py"
ROUTER_HELPER = "restore_admin_disabled_state"
DISK_UPDATE_PATH = "/update_weights_from_disk"
HTTP_CONFLICT = 409
INTERNAL_ASSERTION = "UNIT_TEST_INTERNAL_ASSERTION"


def audit_router_update(bundle: ReviewBundle) -> dict[tuple[str, str], Finding]:
    sources = {file.file_path: file.content for file in bundle.files}
    router_source = sources.get(ROUTER_APP_PATH)
    if router_source is None:
        return {}
    else:
        pass
    try:
        router_module = ast.parse(router_source)
    except (SyntaxError, ValueError):
        return {}
    else:
        pass
    functions = {
        node.name: node
        for node in router_module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    broadcast = functions.get("broadcast_admin_request")
    finalize = functions.get("broadcast_admin_request_locked")
    if broadcast is None or finalize is None:
        return {}
    elif (
        f'@app.post("{DISK_UPDATE_PATH}"' not in router_source
        or "journal.keep([worker.worker_id for worker in workers])"
        not in ast.unparse(finalize)
        or ROUTER_HELPER not in ast.unparse(finalize)
    ):
        return {}
    else:
        retry_gate = next(
            (
                node
                for node in ast.walk(broadcast)
                if isinstance(node, ast.If)
                and ast.unparse(node.test)
                == "journal is not None and journal.has_pending()"
                and isinstance(node.body[0], ast.Return)
                and isinstance(response := node.body[0].value, ast.Call)
                and ast.unparse(response.func) == "error_response"
                and response.args
                and isinstance(response.args[0], ast.Constant)
                and response.args[0].value == HTTP_CONFLICT
            ),
            None,
        )
    if retry_gate is None:
        return {}
    else:
        evidence_start = retry_gate.lineno
        evidence_end = retry_gate.end_lineno or evidence_start

    findings: dict[tuple[str, str], Finding] = {}
    for path in dict.fromkeys(case.file_path for case in bundle.unit_test_cases):
        if not path.startswith("tests/unit_test/router/"):
            continue
        else:
            module = ast.parse(sources[path])
        definitions = test_definitions(module)
        for case in bundle.unit_test_cases:
            if case.file_path != path:
                continue
            else:
                definition = definitions[case.name]
            imports_helper = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "sglang_omni_router.python.app"
                and any(
                    alias.name == ROUTER_HELPER and alias.asname is None
                    for alias in node.names
                )
                for tree in (module, definition)
                for node in tree.body
            )
            if not imports_helper:
                continue
            else:
                statements = definition.body
            if len(statements) < 2 or not isinstance(
                assertion := statements[-1], ast.Assert
            ):
                continue
            elif not isinstance(helper := statements[-2], ast.Expr) or not isinstance(
                call := helper.value, ast.Call
            ):
                continue
            elif (
                ast.unparse(call.func) != ROUTER_HELPER
                or not call.args
                or not isinstance(call.args[0], ast.Name)
                or not any(
                    keyword.arg == "outcome_safe"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                    for keyword in call.keywords
                )
            ):
                continue
            else:
                worker_list = call.args[0].id
            expression = assertion.test
            if (
                not isinstance(expression, ast.Call)
                or ast.unparse(expression.func) != "all"
                or len(expression.args) != 1
                or not isinstance(workers := expression.args[0], ast.GeneratorExp)
                or len(workers.generators) != 1
                or not isinstance(workers.elt, ast.Attribute)
                or workers.elt.attr != "disabled"
                or ast.unparse(workers.elt.value)
                != ast.unparse(workers.generators[0].target)
                or ast.unparse(workers.generators[0].iter) != worker_list
                or workers.generators[0].ifs
                or any(
                    isinstance(node, (ast.Assert, ast.With, ast.AsyncWith))
                    or (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"post", "request"}
                    )
                    for statement in statements[:-2]
                    for node in ast.walk(statement)
                )
            ):
                continue
            else:
                start = helper.lineno
                end = assertion.end_lineno or assertion.lineno
            if not any(
                first <= end and start <= last
                for first, last in case.changed_line_ranges
            ):
                continue
            else:
                pass
            findings[(case.file_path, case.name)] = Finding(
                rule_id=INTERNAL_ASSERTION,
                file_path=path,
                line_start=start,
                line_end=end,
                quote="\n".join(sources[path].splitlines()[start - 1 : end]),
                claim="The test asserts only the helper's in-memory disabled state, leaving the HTTP retry gate uncovered.",
                evidence_file_path=ROUTER_APP_PATH,
                evidence_line_start=evidence_start,
                evidence_line_end=evidence_end,
                evidence_quote="\n".join(
                    router_source.splitlines()[evidence_start - 1 : evidence_end]
                ),
                public_contract=(
                    f"After an interrupted POST {DISK_UPDATE_PATH}, the router refuses "
                    f"another weight update with HTTP {HTTP_CONFLICT} until the unresolved "
                    "update is recovered."
                ),
                regression=(
                    "If the broadcast loses its recovery journal while leaving workers "
                    f"disabled, the helper-only test still passes and a retry can bypass HTTP {HTTP_CONFLICT}."
                ),
                impact="A second update can reach workers whose weight versions are unknown.",
                suggested_fix=(
                    f"Use TestClient to POST {DISK_UPDATE_PATH} with an upstream that raises "
                    "during the first call, then retry and assert "
                    f"retry.status_code == {HTTP_CONFLICT}."
                ),
                change_relation=(
                    "undetermined" if case.previous_source else "introduced"
                ),
                certainty="supported",
            )
    return findings
