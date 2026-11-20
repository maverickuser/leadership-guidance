## Why

Answers from `/ask-coach` (and the MCP `ask` prompt) link a post every time they use one of its points. The links break up the text, so the answer reads like a list of citations instead of a write-up. The user wants each answer to read like a short blog post, with all the sources together at the end.

## What Changes

- The `ask` prompt's instructions (and so `/ask-coach`) ask for a blog-style write-up: a title, an opening that gives the direct answer, a few connected sections in prose, and "Try this week" for advice questions. Posts are credited by name in the text ("as *Post title* puts it") but **not linked there**. All links go in one "Sources" list at the end: title, URL and date.
- The server instructions (`SERVER_INSTRUCTIONS`, used for quick answers without the prompt) say the same: credit posts by name, links only in a closing sources list.
- The coach voice stays: first person, teaching the reader as "you", never claiming experience of its own, saying plainly when the archive doesn't cover something.
- The answer eval keeps the current instructions (prompt version 2, inline links) as its own fixed copy. Its answers, grades and saved labels stay valid and comparable. Citation precision depends on inline links, so the eval keeps them.
- After merging, the user runs `archive mcp --install-command` again to update `~/.claude/commands/ask-coach.md`. The README says so.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-server`: the server instructions and the `ask` prompt (and so the `/ask-coach` command) ask for a blog-style write-up with sources only at the end, instead of inline links.
- `answer-evaluation`: answers are written from the eval's own fixed copy of the version 2 answering instructions, not from the live `ask` prompt.

## Impact

- `src/newsletter_archive/mcp_server.py`: `SERVER_INSTRUCTIONS`, `ASK_INSTRUCTIONS`.
- `src/newsletter_archive/search/answers.py`: `answer_prompt` uses a frozen v2 copy of the instructions; `ANSWER_PROMPT_VERSION` stays 2 and the text stays byte-identical, so cached answers are reused.
- Tests for the prompt text, the command file and the eval prompt.
- `README.md`: the answer format and the reminder to re-run `archive mcp --install-command`.
- No live calls, no index or schema change, no new secrets.
