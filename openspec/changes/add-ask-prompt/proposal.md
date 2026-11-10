## Why

Answers built from the archive come back as bulleted summaries unless the question spells out the format. The user wants a refined answer: several connected paragraphs that answer the question, with each claim linked to the post it came from. Repeating that instruction in every question is tedious and easy to forget.

## What Changes

- **New `ask` prompt on the MCP server.** It takes one argument, `question`. It returns instructions that tell Claude how to research and write the answer:
  - search summaries first to find the relevant posts, then passages of the best posts for evidence and quotes
  - run a few searches when the question has several parts
  - write 3–6 paragraphs of prose, one point per paragraph, opening with the direct answer
  - link the post title inline after each claim, quote the author's words where they help, and end with a short sources list
  - say where posts disagree or the archive doesn't cover something, rather than filling gaps from general knowledge
- **Where it shows up:** Claude Desktop and other MCP clients list it in their prompts menu, with a text box for the question.
- **Claude Code shortcut `/ask-archive <question>`:** Claude Code splits the text after an MCP prompt command on spaces and sends only the first word to a one-argument prompt (verified in Claude Code 2.1.322), so `/mcp__…__ask how do I…` would send just "how". A Claude Code command file receives the whole question. `archive mcp --install-command` writes it to `~/.claude/commands/ask-archive.md`, with the same instructions as the prompt. Rerun it to update.
- **Rename the server to `leadership-coach`:** it describes what the server is for. Only the server's name changes: the `archive` command, the package, the database and the data folder keep theirs. Claude Code needs re-registering under the new name.
- **Server instructions:** they describe the same answer style briefly, so plain questions that don't use the prompt also lean that way.
- **No change to search.** The `search` tool, its results and the read-only, data-only rules stay as they are. The prompt only returns text and never calls a model or the database.
- **Housekeeping first:** archive the finished `add-hybrid-search` change, so `mcp-server` becomes a main spec this change can modify, and replace the placeholder Purpose lines in the main specs.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `mcp-server`: adds the `ask` prompt and the Claude Code `/ask-archive` command (guided answers in prose with source links); renames the server to `leadership-coach` and updates its instructions; allows the prompt's fixed instruction text under "Data only".

## Impact

- **Code:** `src/newsletter_archive/mcp_server.py` (the prompt, the command file writer, the instructions and the server name) and the `archive mcp --install-command` option in `cli.py`.
- **Tests:** unit tests for the prompt's listing and text, and the existing stdio subprocess test extended to fetch the prompt.
- **Docs:** README "Search (MCP server)" section: register as `leadership-coach`, install and use `/ask-archive`, and the prompt in Claude Desktop.
- **Your setup:** after merging, remove `newsletter-archive` from Claude Code and add `leadership-coach` (README commands), then run `archive mcp --install-command`.
- **Specs:** `add-hybrid-search` is archived and its delta specs synced into `openspec/specs/`.
- **Unchanged:** dependencies, cost and secrets. The prompt is static text, so it adds no Gemini calls.
