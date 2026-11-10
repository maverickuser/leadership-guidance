## Why

Answers built from the archive come back as bulleted summaries unless the question spells out the format. The user wants a refined answer: several connected paragraphs that answer the question, with each claim linked to the post it came from. Repeating that instruction in every question is tedious and easy to forget.

## What Changes

- **New `ask` prompt on the MCP server.** It takes one argument, `question`. It returns instructions that tell Claude how to research and write the answer:
  - search summaries first to find the relevant posts, then passages of the best posts for evidence and quotes
  - run a few searches when the question has several parts
  - write 3–6 paragraphs of prose, one point per paragraph, opening with the direct answer
  - link the post title inline after each claim, quote the author's words where they help, and end with a short sources list
  - say where posts disagree or the archive doesn't cover something, rather than filling gaps from general knowledge
- **Where it shows up:** Claude Code lists it as the slash command `/mcp__newsletter-archive__ask`. Claude Desktop lists it in its prompts menu.
- **Server instructions:** they describe the same answer style briefly, so plain questions that don't use the prompt also lean that way.
- **No change to search.** The `search` tool, its results and the read-only, data-only rules stay as they are. The prompt only returns text and never calls a model or the database.
- **Housekeeping first:** archive the finished `add-hybrid-search` change, so `mcp-server` becomes a main spec this change can modify.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `mcp-server`: adds a requirement for an `ask` prompt (guided answers in prose with source links), and updates the server instructions to match.

## Impact

- **Code:** `src/newsletter_archive/mcp_server.py`, about 60 lines: the prompt text and its registration, plus the instructions string.
- **Tests:** unit tests for the prompt's listing and text, and the existing stdio subprocess test extended to fetch the prompt.
- **Docs:** README "Search (MCP server)" section, showing how to use `/mcp__newsletter-archive__ask`.
- **Specs:** `add-hybrid-search` is archived and its delta specs synced into `openspec/specs/`.
- **Unchanged:** dependencies, cost and secrets. The prompt is static text, so it adds no Gemini calls.
