## 0. Approvals

- [x] 0.1 Proposal approved 2026-11-15 (titles linked in the text, Sources at the bottom)
- [x] 0.2 Specs approved 2026-11-15
- [x] 0.3 Design and one-PR plan approved 2026-11-15 (no lld.md: text-only change)

## 1. Coach voice (PR 1)

- [x] 1.1 Rewrite the answer part of `ASK_INSTRUCTIONS`: lesson shape, first person, no invented experience, title links, Try this week, Sources
- [x] 1.2 Match `SERVER_INSTRUCTIONS`, the prompt description and the command file description
- [x] 1.3 Update the prompt, server-instruction and command tests, and check the old "never first person" rule is gone
- [x] 1.4 README: the ask prompt section

## Every PR (definition of done)

- CI green: `test` (3.11, 3.12), `integration`, `coverage` (≥ 95%), `lint`, `safety`
- Tests ship with the code
- Code review run on a lower model, findings fixed in a separate commit
- PR template filled in; the user merges

## After merge

- The user runs `archive mcp --install-command` to update `/ask-coach`
- Optional, with the user's yes (about $1.70): answer eval with a new label, compared with `posts-max-10x3`
