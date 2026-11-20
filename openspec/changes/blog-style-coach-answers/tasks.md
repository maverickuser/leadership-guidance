## 0. Approvals

- [x] 0.1 Proposal approved 2026-11-20
- [x] 0.2 Specs approved 2026-11-20
- [x] 0.3 Design approved 2026-11-20
- [x] 0.4 Low-level design approved 2026-11-20
- [x] 0.5 PR plan approved 2026-11-20

## 1. Eval: freeze the version 2 answering instructions (PR 1)

- [x] 1.1 `answers.py`: add `ANSWER_INSTRUCTIONS`, today's `ASK_INSTRUCTIONS` copied exactly, and export it
- [x] 1.2 `answer_prompt` builds the prompt from it; drop the `mcp_server.ask_message` import
- [x] 1.3 Test: the SHA-256 pin, version 2, inline-link text; the prompt string is unchanged for a sample question
- [x] 1.4 Integration test comment; README answer-eval note

## 2. Ask prompt: blog-style write-up, sources at the end (PR 2)

- [x] 2.1 `mcp_server.py`: new `ASK_INSTRUCTIONS` writing block
- [x] 2.2 `mcp_server.py`: new `SERVER_INSTRUCTIONS`
- [x] 2.3 Tests: prompt phrases, links only after "### Sources", server instructions; the eval copy now differs from the live prompt
- [x] 2.4 README "Ask for a full answer": new format, and re-run `archive mcp --install-command`

## 3. After merge

- [ ] 3.1 User runs `archive mcp --install-command`
- [ ] 3.2 Sync specs and archive the change

## Every PR (definition of done)

- CI green: `test` (3.11, 3.12), `integration`, `coverage` (≥ 95%), `lint`, `safety`
- Tests ship with the code
- `/code-review high` run (Sonnet subagent), fixes in a `Fix review findings on the <area>` commit
- PR template filled; tasks ticked; the user merges
