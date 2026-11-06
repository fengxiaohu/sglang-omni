# Pull request labels

The PR Labeler suggests existing labels from changed paths, the PR title, and
the head branch name. Title and branch matching is case-insensitive. Multiple labels may match.
The workflow adds labels but does not remove them, including after a title,
branch, or file change. A matching rule can add a manually removed label again
on a later run.

| Label | Example title | Example branch |
| --- | --- | --- |
| `accessibility` | `Improve accessibility` | `a11y/keyboard` |
| `bug` | `Fix: incorrect timeout` or `Fix a regression` | `fix/timeout` |
| `duplicate` | `Duplicate of #123` or `Duplicate: existing PR` | `duplicate/123` |
| `enhancement` | `Feature: add streaming` | `feature/streaming` |
| `good first issue` | `Good first issue: improve error message` | `good-first-issue/message` |
| `help wanted` | `Help wanted: reproduce a failure` | `help-wanted/reproduction` |
| `invalid` | `Invalid: unrelated PR` or `[invalid] Unrelated` | `invalid/unrelated` |
| `question` | `Question: which backend?` or `[question] Backend choice` | `question/backend` |
| `wontfix` | `Won't fix: unsupported platform` | `wontfix/platform` |

The `documentation` label matches changes under `docs/`, the root
`README.md` or `CONTRIBUTING.md`, or `.github/docs-review-pilot.md`.

These labels are suggestions based on text, not verified decisions about a
duplicate, an invalid PR, or whether work will be accepted. Maintainers should
review them. The workflow does not add CI labels or analyze PR content with a
model.
