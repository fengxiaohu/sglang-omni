import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW_PATH = (
    Path(__file__).resolve().parents[3]
    / ".github/workflows/pr-documentation-labeler.yml"
)
NODE_RUNNER = r"""
const {readFileSync} = require('node:fs');
const input = JSON.parse(readFileSync(0, 'utf8'));
process.env.PR_NUMBER = '42';
let labels = [];
const github = {
  rest: {
    pulls: {get: async () => ({data: {title: input.title, head: {ref: input.branch}}})},
    issues: {addLabels: async request => {labels = request.labels}},
  },
};
const context = {repo: {owner: 'example', repo: 'example'}};
const core = {info: () => {}};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
(async () => {
  await new AsyncFunction('github', 'context', 'core', input.script)(github, context, core);
  process.stdout.write(JSON.stringify(labels));
})().catch(error => {console.error(error); process.exit(1)});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
@pytest.mark.parametrize(
    ("title", "branch", "expected_labels"),
    [
        ("Improve accessibility", "probe/neutral", ["accessibility"]),
        ("Unrelated change", "codex/a11y/keyboard", ["accessibility"]),
        ("Fix: incorrect timeout", "probe/neutral", ["bug"]),
        ("Unrelated change", "codex/bugfix/timeout", ["bug"]),
        ("Duplicate of #123", "probe/neutral", ["duplicate"]),
        ("Unrelated change", "codex/duplicate/123", ["duplicate"]),
        ("Feature: add streaming", "probe/neutral", ["enhancement"]),
        ("Unrelated change", "codex/feat/streaming", ["enhancement"]),
        ("Good first issue: improve messages", "probe/neutral", ["good first issue"]),
        ("Unrelated change", "codex/good-first-issue/message", ["good first issue"]),
        ("Help wanted: reproduce a failure", "probe/neutral", ["help wanted"]),
        ("Unrelated change", "codex/help-wanted/reproduction", ["help wanted"]),
        ("Invalid: unrelated PR", "probe/neutral", ["invalid"]),
        ("[invalid] Unrelated PR", "probe/neutral", ["invalid"]),
        ("Unrelated change", "codex/invalid/unrelated", ["invalid"]),
        ("Question: which backend?", "probe/neutral", ["question"]),
        ("[question] Backend choice", "probe/neutral", ["question"]),
        ("Unrelated change", "codex/question/backend", ["question"]),
        ("Won't fix: unsupported platform", "probe/neutral", ["wontfix"]),
        ("Unrelated change", "codex/wontfix/platform", ["wontfix"]),
        ("Fix: invalid input", "codex/fix/invalid-input", ["bug"]),
        ("Fix: duplicate response", "codex/fix/duplicate-response", ["bug"]),
        ("Accessibility feature", "probe/neutral", ["accessibility", "enhancement"]),
        ("Use run-ci when needed", "probe/neutral", []),
        ("$(touch /tmp/labeler-injection)", "probe/neutral", []),
    ],
)
def test_suggested_labels(title: str, branch: str, expected_labels: list[str]) -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text())
    script = workflow["jobs"]["label"]["steps"][1]["with"]["script"]
    input_json = json.dumps({"script": script, "title": title, "branch": branch})
    result = subprocess.run(
        ["node", "-e", NODE_RUNNER],
        input=input_json,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == expected_labels
