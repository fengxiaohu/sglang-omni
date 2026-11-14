# Docs review Actions pilot

Repository members with write access can run **Advisory Docs Review** from the
default branch and enter the number of any open PR with documentation changes.
The workflow reads fixed base and head commits and loads the reviewer from the
default branch. The review is advisory and uses PR files as data.

## Configuration

A collection-only run uses no model credentials. For live reviews, set the
repository secret `DOCS_REVIEW_API_KEY` and these repository variables.

- `DOCS_REVIEW_MODEL` is a JSON object with `model_name`, `endpoint_url`,
  `max_output_tokens`, and `api_key_environment_variable`. Set the last field
  to `DOCS_REVIEW_API_KEY`. Set `reasoning_effort` when the configured model
  supports it.
- `DOCS_REVIEW_BUDGET` holds `call_cost_cents`, `max_cost_cents_per_call`,
  `max_cost_cents_per_pull_request`, and `max_cost_cents_per_day`. Use a positive
  estimate for each call. The workflow reserves estimated costs before a call,
  counts failed and interrupted calls, and enforces the daily limit in UTC.

Create a `docs-review-ledger` branch with a root-level
`docs-review-ledger.json` containing `{"version":1,"reservations":[]}` before
the first live review. Keep the branch across runs so the workflow can track
reservations and limit each PR to two calls. This file stores reservation
metadata. The workflow permissions allow it to update that branch.
Dispatch reviews one at a time. GitHub keeps one pending run for this
concurrency group and replaces it if another run arrives.

## Run and publish

Start with a collection-only run to inspect the selected files.

```sh
gh workflow run docs-review-pilot.yml -f pull_request_number=123 -f offline=true
```

Set `offline=false` to authorize one live call. Use `follow_up=true` to authorize
the second call for a PR. Live reviews send the selected documents, related
code, and the PR description to the configured model provider.
The pilot caps live review inputs at 100,000 bytes. Use the local review tool
for collection and inspection when a change exceeds the pilot's limit.

Download the `docs-review-RUN_ID-ATTEMPT` artifact and open `report.md`. It numbers
the findings in the same order as `report.findings` in `report.json`. Both
files are plaintext and are downloadable by signed-in users with repository
read access. They contain the report metadata, advisory findings, and citations.

Run **Publish Selected Docs Review Findings** with the run ID, the attempt
number you inspected, and the comma-separated numbers to post in one PR comment.

```sh
gh workflow run docs-review-publish.yml -f review_run_id=RUN_ID \
  -f review_run_attempt=ATTEMPT -f finding_numbers=1,2
```

Publication requires a successful model-backed run and an open PR at the
reviewed base and head commits. If either revision changes during publication,
the workflow marks the comment stale.
