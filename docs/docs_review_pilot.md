# Advisory documentation review pilot

This maintainer-triggered workflow reviews documentation changes without executing
pull request code. It is advisory and does not approve, merge, close, or post to a
pull request. It runs only from the default branch. A reviewer must inspect and
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
