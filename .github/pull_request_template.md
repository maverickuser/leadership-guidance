## Stack

<!-- List every PR in the stack, bottom to top. Mark this one with 👉 -->
- [1/10] ...

## What

<!-- Summary, plus the tasks.md IDs this PR covers -->

## Spec

<!-- Requirements / scenarios from openspec/specs/ (or the active change under openspec/changes/) implemented here -->

## Tests

<!-- New unit / integration tests, plus the pytest summary line -->

## Safety checklist

- [ ] No cookie values, `.env`, or `ghost-members-ssr=` values anywhere in the diff
- [ ] Nothing under `data/` is tracked
- [ ] Fixtures are structure-only (no real article prose, no member name/email/uuid)
- [ ] Full request headers are never logged
- [ ] No API keys or database passwords in the diff, logs, error messages or MCP responses

## Review checklist

- [ ] Combined coverage (unit + integration) ≥ 95%; new modules ≥ 95%. Coverage line:
- [ ] New code has unit tests; database code has `db` integration tests
- [ ] `/code-review high` run and findings fixed
- [ ] tasks.md items for this PR checked off

## Out of scope

<!-- What lands in a later PR -->
