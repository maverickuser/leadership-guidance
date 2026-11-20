## Context

`ASK_INSTRUCTIONS` in `mcp_server.py` is the one text behind three things: the MCP `ask` prompt, the `/ask-coach` command file (written by `archive mcp --install-command`), and the answer eval (`answers.answer_prompt` calls `mcp_server.ask_message`). Today it asks for an inline link each time a post's point is used. `SERVER_INSTRUCTIONS` says the same for answers written without the prompt.

The answer eval relies on those inline links. Citation precision is computed from the answer's Markdown links, and the judge ties each claim to the post linked for it. Its saved answers are keyed by question, `ANSWER_PROMPT_VERSION` (2), model, thinking level and result ids, not by the prompt text.

## Goals / Non-Goals

**Goals**
- `/ask-coach`, the `ask` prompt and plain MCP answers read like a short blog post, with links only in a closing Sources list.
- The answer eval keeps measuring the same thing, so the v2 runs and labels stay valid.

**Non-Goals**
- A new eval prompt version or new citation metrics for end-only links.
- Changing the search tool, the research steps or the coach voice.
- Updating `~/.claude/commands/ask-coach.md` automatically. The user re-runs `archive mcp --install-command`.

## Decisions

### Decisions (made with the user)
- Blog-style write-up; posts named in the text, no inline links; every link in the final Sources list (2026-11-20).
- The answer eval stays on the v2 inline-link instructions (2026-11-20).
- The rule lives in the repo's prompt text, not in a separate Claude Code skill. One source keeps the MCP prompt and the command the same, and the command works from any folder (2026-11-20).

### 1. The eval gets its own frozen copy of the v2 text
`answers.py` holds `ANSWER_INSTRUCTIONS_V2`, a byte-for-byte copy of today's `ASK_INSTRUCTIONS`, and `answer_prompt` builds `Question: {question}\n\n{ANSWER_INSTRUCTIONS_V2}` itself. `ANSWER_PROMPT_VERSION` stays 2. A test pins the copy's SHA-256 so nobody edits it without bumping the version.

*Alternative:* keep using `ask_message` and accept v3. That makes every saved answer stale and the scores not comparable, which the user declined.

*Side effect:* `answers.py` no longer imports the MCP module, so the eval no longer loads the MCP SDK. The eval's questions come from the user's own file, so the `ask` prompt's empty or too-long checks aren't needed there.

### 2. New `ASK_INSTRUCTIONS`
The research steps stay word for word. The writing part changes to:
- "Write the answer as a short write-up, like a blog post from a coach teaching the reader."
- A `## ` title, an opening paragraph with the problem and the direct answer, then a few `### ` sections that lead into each other, in prose.
- Credit each point by naming its post in the text (for example *as "Post title" puts it*), **with no link**. Quotes stay brief, in quotation marks, attributed to their post.
- "Put no links anywhere except the Sources section."
- "Try this week" stays: two to four actions, each naming its post, no links. Skip it for a narrow question.
- Finish with `### Sources`: each post as `[Title](URL)` and its date.
The coach voice lines (first person, "you", never claiming experience, posts disagreeing, gaps said plainly) are unchanged.

### 3. New `SERVER_INSTRUCTIONS`
"...Name the posts you draw on in the text, without links, and end with a sources list that links each one by title and URL..." The rest stays. The `ask` prompt's description ("lesson from a coach") stays as it is.

### 4. The command file
No code change. `--install-command` already rewrites a file it wrote before. The README tells the user to run it again after this update.

## Risks / Trade-offs

- **The eval no longer tests the exact live prompt.** It measures retrieval and answer quality with the v2 writing rules. Search, settings and model are still the live ones. A later change can add an end-only citation measure and move the eval to v3.
- **Models may still add inline links.** The prompt says it twice (the credit rule and the "no links except Sources" rule). This is only checked by reading answers; no live call is part of this change.

## Migration Plan

Merge, then run `archive mcp --install-command` once. Rollback: revert the PR and run it again.
