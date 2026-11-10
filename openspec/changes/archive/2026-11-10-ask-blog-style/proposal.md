## Why

Answers from the `ask` prompt (and `/ask-coach`) read like a report about the newsletter:
"the author argues…", "the post says…". The user wants answers that teach the reader directly,
the way a good blog post does: written to "you", with a title and short sections, and with the
newsletter's own words quoted and linked where they help. They tried this style on "how do I give
implicit feedback?" and approved it.

## What Changes

- The `ask` prompt's writing instructions change from "3 to 6 paragraphs of prose about the posts"
  to a short teaching blog post:
  - a title that names the topic, then the direct answer in the opening paragraph;
  - short section headings (`###`), one main point per section, a few sentences each;
  - written to the reader ("you") as practical guidance, not as a summary of what the author said;
    no impersonating the author (no first person as the author);
  - quotes from posts kept short, in quotation marks, attributed in plain words ("as one
    engineering leader puts it"), with the post linked inline as [Post title](URL);
  - mostly prose; a few short examples are fine, but no long bullet lists;
  - unchanged: say where posts disagree, say plainly what the archive doesn't cover, never fill
    gaps from general knowledge, end with a "Sources" list with dates.
- The research steps stay the same.
- The server's short instructions (shown to every agent) change "Answer in paragraphs" to match.
- `/ask-coach` picks up the new text automatically; the user re-runs
  `archive mcp --install-command` to update the installed file. README notes this.
- Archive the merged `add-ask-prompt` change first, so the `ask` requirement lives in
  `openspec/specs/mcp-server` and this change can modify it.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `mcp-server`: the "Ask prompt" requirement's writing instructions (blog style instead of
  3 to 6 paragraphs), the "Local MCP server" requirement's wording about how agents answer, and
  the matching scenarios.

## Impact

- Code: `src/newsletter_archive/mcp_server.py` (`ASK_INSTRUCTIONS`, `SERVER_INSTRUCTIONS`, the
  prompt and command descriptions). Text only; no new logic, dependencies, or config.
- Tests: `tests/search/test_mcp_server.py` assertions on the instruction text.
- Docs: README section on `/ask-coach` (re-run `--install-command` after upgrading).
- No live calls, no database or index changes, no secrets touched.
- One PR, after archiving `add-ask-prompt`.
