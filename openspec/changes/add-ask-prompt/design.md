## Context

The MCP server (`archive mcp`, built in `add-hybrid-search`) has one tool, `search`, which returns archive data only: passages and stored post summaries. The calling agent (Claude Code or Claude Desktop) writes the answer. Without guidance, the agent picks its own format, often a bulleted summary. The user wants answers as connected paragraphs with each claim linked to its post.

MCP has a built-in feature for this: **prompts**, which are named message templates a server offers and clients show to the user. The installed SDK (`mcp` 2.3.0) registers one with the `@server.prompt()` decorator. Claude Code shows server prompts as slash commands named `/mcp__<server>__<prompt>`.

## Goals / Non-Goals

**Goals:**
- One command that produces a well-researched answer in prose, with inline source links, from any MCP client that supports prompts.
- Nudge plain questions (without the command) toward the same style.

**Non-Goals:**
- Generating answers on the server. The server stays data-only and calls no model.
- Changing search, ranking or the `search` tool.
- A tool for reading a whole post (option 3 from the discussion; can come later).

## Decisions (made with the user)

1. **An MCP prompt, not a server-side answer generator** (2026-11-10). The agent already writes well. A server-side generator would add API cost, a second model to tune, and code, with no better answers.
2. **Answer format** (approved in the proposal): 3–6 paragraphs, direct answer first, one point per paragraph, inline title links, quotes where they help, sources list at the end, disagreements and gaps stated plainly.
9. **Claude Code shortcut `/ask-coach`** (2026-11-10, after review). Claude Code 2.1.322 splits an MCP prompt command's text on whitespace and maps words to argument names by position (`D.trim().split(/\s+/)`), so a one-argument prompt gets only the first word. Options shown to the user: a Claude Code command file (chosen), two steps (`/…__ask` alone, then the question), or no command. A command file gets the whole text as `$ARGUMENTS`. Named `/ask-coach` to match the server name (user's choice).
10. **Rename the server to `leadership-coach`** (2026-11-10). The name describes what it's for. Only `SERVER_NAME` and the docs change; the package, CLI, database and data folder keep "newsletter-archive". A hyphen, not an underscore, by the user's choice.

## Decisions (technical)

3. **Static text.** The prompt function only formats the question into a fixed instruction text. It never searches or touches the database. So fetching it is instant, can't fail on a stopped database, and the "data only" requirement holds.
4. **Question in the message, at the top.** The message starts with the question, then the instructions. (Claude Code does not pass the whole text to the prompt; see decision 9.)
5. **Clear errors.** An empty question, or one over the limit in decision 13, is rejected with a message the user can act on.
6. **Results are data, not instructions.** The prompt tells the agent to treat search results as source material only. Post text is the author's, but this keeps any instruction-like sentence in a post from steering the answer.
7. **Server instructions stay short.** Agents see them with every session, so they get one extra sentence: answer in paragraphs with inline post links, and use the `ask` prompt for a full answer.
8. **Archive `add-hybrid-search` first.** `mcp-server` exists only in that change's delta specs. Syncing and archiving it gives this change a main spec to modify.
11. **One source for the instructions.** The prompt and the command file are built from the same `ASK_INSTRUCTIONS` text. The command file starts with a marker line, so `--install-command` can tell its own file (update it) from someone else's (leave it, unless `--force`).
12. **Research wording matches the tool.** `search` has no per-post filter, so the instructions say "prefer passages from those posts" rather than "search those posts". They match the tool description: broad questions start with summaries, and narrow factual ones may go straight to passages.
13. **Own length limit.** The question isn't sent to `search` as-is (the agent writes its own queries), so it gets its own, looser limit of 4,000 characters instead of the 1,000-character query limit.

## Risks / Trade-offs

- **Clients without prompt support:** they won't show `ask`. Mitigation: the server instructions still steer plain questions.
- **The command file can go stale:** if `ASK_INSTRUCTIONS` changes, an installed `/ask-coach` keeps the old text until `--install-command` is run again. The README says so.
- **Renaming breaks the old registration:** Claude Code keeps `newsletter-archive` until it's removed and re-added. The README gives the two commands.
- **More searches per answer:** a multi-part question may run 3–5 searches at ~5 s each, so a slower answer. That's the trade for depth, and the prompt says "a few", not unlimited.
- **The agent may still drift from the format:** prompts guide, they don't enforce. If that happens, tune the prompt text; no code shape changes.
