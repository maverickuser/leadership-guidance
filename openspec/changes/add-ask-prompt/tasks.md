## 0. Approvals

- [x] 0.1 Proposal approved 2026-11-10
- [x] 0.2 Specs approved 2026-11-10
- [x] 0.3 Design approved 2026-11-10
- [x] 0.4 Low-level design approved 2026-11-10
- [x] 0.5 PR plan approved 2026-11-10

## 1. Ask prompt (PR 1)

- [x] 1.1 Archive `add-hybrid-search` (sync its delta specs into `openspec/specs/`), as its own commit
- [x] 1.2 `ASK_INSTRUCTIONS`, `ask_message()` with the empty and too-long checks (`MCPError`, `INVALID_PARAMS`)
- [x] 1.3 Register the `ask` prompt in `build_server()`; log the question length only
- [x] 1.4 Update the server instructions
- [x] 1.5 Unit tests: list, fetch, errors, no service access, logs, instructions
- [x] 1.6 Extend the stdio subprocess test to list and fetch `ask`
- [x] 1.7 README: "Ask for a full answer"

## Every PR (definition of done)

- CI green: `test` (3.11, 3.12), `integration`, `coverage` (≥ 95%), `lint`, `safety`
- Tests ship with the code; `mcp_server.py` stays ≥ 95% covered
- `/code-review high` run and every finding fixed (or declined, with the reason in the PR)
- PR template filled in; the user merges
