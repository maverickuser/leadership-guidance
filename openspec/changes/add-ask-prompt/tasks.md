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

## 2. Review fixes, rename and Claude Code command (same PR, after review)

- [x] 2.0 Plan updated with the user's decisions: rename to `leadership-coach`, `/ask-coach` command (2026-11-10)
- [x] 2.1 Rename the server to `leadership-coach` (`SERVER_NAME`, README registration and Desktop config, migration commands)
- [x] 2.2 `install_command()` writing `ask-coach.md` (marker, update, refuse someone else's file unless `--force`, `CLAUDE_CONFIG_DIR`)
- [x] 2.3 `archive mcp --install-command [--force]`, without loading the config
- [x] 2.4 Instructions: wording matches the tool (broad → summaries; prefer passages from those posts); asks for the question if it's missing
- [x] 2.5 Own question limit (4,000); log stripped length, and log rejections with a reason
- [x] 2.6 Test guard records touches instead of raising
- [x] 2.7 Replace the placeholder Purpose lines in the 8 main specs
- [x] 2.8 Tests for 2.1–2.6; README "Ask for a full answer" rewritten

## Every PR (definition of done)

- CI green: `test` (3.11, 3.12), `integration`, `coverage` (≥ 95%), `lint`, `safety`
- Tests ship with the code; `mcp_server.py` stays ≥ 95% covered
- `/code-review high` run and every finding fixed (or declined, with the reason in the PR)
- PR template filled in; the user merges
