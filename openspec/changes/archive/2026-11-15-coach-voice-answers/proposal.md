## Why

Answers from `/ask-coach` and the `ask` prompt read like a summary of what the posts say. The user wants them to read like a lesson: a coach speaking in the first person to a reader who is learning, with the sources at the bottom. A sample written this way (delegating without losing quality or updates) was reviewed and approved on 2026-11-15.

## What Changes

- **Voice.** The answer is written by a coach who teaches in the first person ("I") and speaks to the reader as "you". This replaces the rule "never write as the author in the first person".
- **No invented experience.** The coach never claims stories, jobs or results of its own. Experience and examples come from the posts and are credited to them ("one post puts it this way").
- **Shape of a lesson.** Open with the problem the reader has, then give the direct answer. Teach one idea per section. End with a short "Try this week" list of a few small actions, except for narrow factual questions.
- **Sources at the bottom, titles linked in the text.** When the coach draws on a post, it names the post by title in the text, as a Markdown link. The full Sources list, with dates, closes the answer. (Decided at gate 1: the answer eval keeps working unchanged.)
- Kept as they are: the research steps, attributed quotes, saying where posts disagree, saying plainly when the archive doesn't cover something, and the narrow-question short form.
- The server's short instructions to every client (`SERVER_INSTRUCTIONS`) change the same way.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-server`: the Ask prompt requirement changes its voice, structure and citation rules, and so does the "Not the author's voice" scenario. The Claude Code ask command keeps using the same text, so `archive mcp --install-command` has to be run again to pick it up.

## Impact

- `src/newsletter_archive/mcp_server.py`: `ASK_INSTRUCTIONS`, `SERVER_INSTRUCTIONS`, and the prompt description if it names the voice.
- `tests/search/test_mcp_server.py`: the checks on the prompt text.
- README: the ask prompt section.
- **Answer evaluation.** It reuses this prompt, so new answer runs use the new voice. Citations and post coverage are counted from Markdown links, including those in the Sources list, so the metrics still work. A fresh run (about $1.70 in Gemini, only with the user's yes) would show whether the new voice changes post coverage against `posts-max-10x3`.
- **Faithfulness judge.** It is unchanged, because each claim still links the post it draws on.
- After the merge, the user runs `archive mcp --install-command` to update `~/.claude/commands/ask-coach.md`.
