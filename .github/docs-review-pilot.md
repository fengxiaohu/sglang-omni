# Advisory documentation review pilot

This maintainer-triggered workflow reviews documentation changes without executing
pull request code. It is advisory and does not approve, merge, close, or post to a
pull request. The default branch supports both review scopes. A reviewer must inspect and
choose any result before publishing it.

The Actions workflow has an offline mode for checking collection without a model
call. A model-backed run requires repository variables `DOCS_REVIEW_MODEL` and
`DOCS_REVIEW_BUDGET`, and repository secrets `TEST1` (the model API key) and
`DOCS_REVIEW_REPORT_KEY`. The latter is a Fernet key used to encrypt the complete
report and inputs before upload. Keep a copy outside GitHub; GitHub does not reveal
saved secrets. Artifacts expire after 30 days. Rotating the report key does not
re-encrypt older artifacts.

Only maintainers with repository write access can dispatch the workflow. The
dedicated `codex/docs-review-ledger` branch stores reservations, not report
contents. The workflow reserves the configured estimated cost before a model call;
interrupted and failed calls remain charged to the local limits. Runs are
serialized and the ledger update uses the current Git blob SHA. A completed input
is not called again. A second review, including a retry of an incomplete call,
requires explicit follow-up authorization. At most two reviews are reserved for
a pull request. Ledger deletion or manual modification can invalidate these
protections.

Configured costs are estimates in cents, not the provider's actual charges or a
provider-enforced spending cap. Set a separate limit on the model account. The
workflow limits the rendered input to 100,000 bytes and output tokens to the model
configuration. Ensure the per-call estimate covers the selected model's pricing.

To start a collection-only test:

```sh
gh workflow run docs-review-pilot.yml --repo OWNER/REPO --ref main \
  -f pull_request_number=1 -f offline=true -f follow_up=false \
  -f include_pr_body=false
```

After checking the collection-only result, set `offline=false` to authorize one
model call. Set `follow_up=true` only when authorizing a second review or retry.
For an unbiased test, omit a PR description that already gives away the answer.

After downloading the run's artifact, decrypt it locally with the saved report key
in the `DOCS_REVIEW_REPORT_KEY` environment variable:

```sh
python -c 'import os, pathlib; from cryptography.fernet import Fernet; p=pathlib.Path("report.fernet"); pathlib.Path("report.json").write_bytes(Fernet(os.environ["DOCS_REVIEW_REPORT_KEY"].encode()).decrypt(p.read_bytes()))'
```

The decrypted JSON contains the Markdown report, structured report, and frozen
input bundle. Treat it as private. A report for an earlier base or head is marked
stale. An incomplete review is not a clean result. PR text, checks, or evidence
not included in the input must not be treated as having been reviewed.

After inspecting the report, a maintainer may select findings by their 1-based
position in the JSON `report.findings` list and publish only those findings:

```sh
gh workflow run docs-review-publish.yml --repo OWNER/REPO --ref main \
  -f review_run_id=RUN_ID -f finding_numbers=1,2
```

The publishing workflow refuses an incomplete report or a changed base or head.
It labels the comment with the reviewed commit and attempts to mark it stale if
the pull request changes during publication. Findings remain advisory.

## Unit-test contract review

Choose `review_scope=unit_tests` to review added and edited Python test cases under `tests/unit_test/`. The report uses the test and its caller-visible contract to identify a concrete regression, show the relevant lines, and propose an assertion that would catch it. A duplicate finding must also cite the other test. Missing contract evidence leaves the review incomplete.

The collector reads fixed Git revisions and selects direct edits to test functions and their own decorators. Edits confined to shared fixtures, imports, or class and module settings do not select a test by themselves. The rest of the changed file and imports referenced by the selected cases provide context. Use `unit_test_context_paths` to select additional source files, docs, or neighboring tests, one path per line. The report shows existing GitHub check summaries for the reviewed head where available. Check results provide context for the review. The workflow never runs the PR's tests.

Run locally with the same model and budget options as the documentation reviewer:

```sh
python -m scripts.review_docs review --review-scope unit_tests \
  --repository . --base-commit BASE_SHA --head-commit HEAD_SHA \
  --pull-request-number PR_NUMBER --include-path path/to/contract.py \
  --ledger /tmp/unit-test-review.sqlite --output-directory /tmp/unit-test-review
```

The reviewed files and changed case names appear in the output bundle. Supply `--model-file` and `--budget-file` for a semantic review.

The maintainer can dispatch `docs-review-pilot.yml` with `review_scope=unit_tests` and inspect the encrypted report before choosing findings. A maintainer can set the repository variable `UNIT_TEST_REVIEW_PILOT_REF` to a trusted branch's full Git ref for an isolated unit-test pilot and configure `UNIT_TEST_REVIEW_PILOT_REPORT_KEY` for its reports. Publish selected findings from the default branch after the reviewer is integrated there.

## Automatic issue review

The issue workflow runs on opened, edited, reopened, labeled, and unlabeled issues.
It identifies bug reports and feature requests from their template title, labels,
form headings, or the model's classification. Unclear issue types are left to
maintainers. The model reviews
the issue against required fields and the English request in the corresponding
form. It does not enforce optional fields, close issues, or execute issue content.
Issue titles and bodies are sent to the configured model provider.

The bot posts only suggestions tied to the form. It updates its existing comment
after an edit, and marks prior suggestions as potentially stale if a later review
cannot complete. Duplicate inputs reuse a recorded result. At most two model calls
are reserved per issue; the PR per-item budget is also applied per issue. Issue
and PR calls share the same daily budget and serialized ledger. A new UTC day
starts at 08:00 in Asia/Shanghai. Estimates are not provider-enforced spending
limits. Unavailable models, malformed results, and exhausted budgets are not
reported as successful reviews.

A maintainer can dispatch `issue-review.yml` for an existing issue. To retry an
incomplete review once, set `retry_failed` to true; the same item and daily
limits still apply. Disable the workflow to stop automatic issue reviews.
