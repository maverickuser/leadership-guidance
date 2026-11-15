## 0. Approvals

- [x] 0.1 Proposal approved 2026-11-15
- [x] 0.2 Specs and one-PR plan approved 2026-11-15 (no design.md or lld.md: a small fix)

## 1. Match links with or without www (PR 1)

- [x] 1.1 `normalize_url` drops a leading `www.`; `citations` matches stored keys with the current rules
- [x] 1.2 A reused grade recomputes citation precision and post coverage, and keeps the judge's claims and grade (`recount`)
- [x] 1.3 Tests: links with or without `www.`, keys saved before the fix, recount on reuse

## Every PR (definition of done)

- CI green: `test` (3.11, 3.12), `integration`, `coverage` (≥ 95%), `lint`, `safety`
- Code review on a lower model, findings fixed in a separate commit
- PR template filled in

## After merge

- Re-run `--answers --label posts-max-10x3` and `--label coach-voice` (no Gemini calls), then `--compare`
