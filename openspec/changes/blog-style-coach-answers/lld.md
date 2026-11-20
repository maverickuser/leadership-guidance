# Low-level design: blog-style coach answers

## 1. Measured facts

- Today's `ASK_INSTRUCTIONS` is 1,986 characters, SHA-256 `5676230a60588cc80801f0c54812315f740c0cf47f64f93233acd9533e4dad1b` (measured on `main` at d39eeb2). The frozen eval copy must hash to this.
- Saved answers are keyed by `_sha(question, ANSWER_PROMPT_VERSION, model, thinking_level, sorted(result_ids))` (`_sha` in `answer_key`). The prompt text isn't in the key, so the pinned hash is what guarantees the reused answers came from the same text.
- `answer_prompt` was the only caller of `mcp_server.ask_message` outside the MCP server.

## 2. `search/answers.py`

```python
ANSWER_PROMPT_VERSION = 2  # bump when ANSWER_INSTRUCTIONS changes; answers are then made again

# The answering instructions the eval uses: a frozen copy of the `ask` prompt as of version 2
# (inline links, so citation precision can be measured). The live `ask` prompt in mcp_server
# may change; this text only changes together with ANSWER_PROMPT_VERSION.
ANSWER_INSTRUCTIONS = """Answer the question above from the leadership-coach newsletter archive,
...today's text, byte for byte..."""

def answer_prompt(question: str, retrieved: Retrieved) -> str:
    """The eval's answering instructions (question first, as the `ask` prompt does), then the
    search results."""
    return f"Question: {question.strip()}\n\n{ANSWER_INSTRUCTIONS}\n\nSearch results:\n\n{render_results(retrieved)}"
```

- The name is `ANSWER_INSTRUCTIONS` (not `_V2`), next to `ANSWER_PROMPT_VERSION`, which already says the version. Added to `__all__`.
- The lazy `from ..mcp_server import ask_message` goes away.
- The output for any non-empty question up to 4,000 characters is identical to today's.

## 3. `mcp_server.py`

`SERVER_INSTRUCTIONS`:

> Search a personal newsletter archive with the `search` tool. Answer advice questions as a short write-up from a coach, in the first person to the reader ("you"), never claiming experience of your own; answer quick lookups briefly. Name the posts you draw on in the text, without links, and end with a sources list that links each one by title and URL. For a full researched answer, use the `ask` prompt (in Claude Code: `/ask-coach <question>`).

`ASK_INSTRUCTIONS`: the opening line and the "Research:" block stay as they are. The writing block becomes:

```
Write the answer as a short write-up, like a blog post from a coach teaching the reader:
- Start with a "## " title that names the topic. Then one paragraph that names the problem the
  reader faces and gives the direct answer.
- Then a few short sections with "### " headings, one idea each, a few sentences each, that lead
  from one to the next. Write prose; a few short examples are fine, but no long bullet lists.
- Write as the coach, in the first person ("I"), teaching the reader as "you" with practical
  guidance, rather than summarizing the posts one by one.
- Never claim experiences, stories, jobs or results of your own. Experience and examples come
  from the posts; credit them ("as one post puts it").
- Credit each point to its post by naming the post's title in the text, in quotation marks (as
  "Post title" puts it). Don't link it there. Quote the posts' own words briefly, in quotation
  marks, attributed to the post they come from; don't invent who the speaker is.
- Put no links anywhere in the answer except the Sources section at the end.
- Where posts disagree or show different sides, say what each post says and explain the
  difference.
- If the archive doesn't cover part of the question, say so plainly. Don't fill the gap from
  general knowledge.
- Then "### Try this week": two to four small, concrete actions, each naming the post it comes
  from. Skip it for a narrow, factual question.
- Finish with "### Sources": each post used, as [Title](URL), with its date.
A narrow, factual question needs only the title, the direct answer and the Sources list.
```

The `ask` prompt's description and the command file's description say "write-up from a coach" with sources at the end. The `search` tool's description changes "Always cite the post title and URL" to keeping each post's title and URL, naming posts in the text and linking them only in a closing sources list, so it doesn't contradict the server instructions. `ask_message` and the command file code are unchanged.

## 4. Errors and secrets

No new errors, inputs, logs or secrets. The `ask` prompt still logs only the question's length.

## 5. Tests

`tests/search/test_mcp_server.py`
- `test_ask_prompt_returns_the_question_and_how_to_answer`: swap the inline-link phrases (`"Each time you use a point from a post"`, `"[Post title](URL)"`) for `"like a blog post"`, `"naming the post's title in the text"`, `"Don't link it there"` and `"no links anywhere in the answer except the Sources section"`. Assert that `"[Post title](URL)"` and `"as a link right there"` are gone, and that `[Title](URL)` appears only after `"### Sources"`. Keep the voice, order and narrow-question checks. Tagged `# spec: mcp-server / Ask prompt (Sources only at the end)`.
- New `test_server_instructions_put_links_only_at_the_end`: the instructions say "without links" and "end with a sources list", and no longer say "Link each post". Tagged `# spec: mcp-server / Local MCP server (Sources only at the end)`.
- The existing command-file test (`ASK_INSTRUCTIONS` at the end of the file) still covers `/ask-coach` getting the new text.

`tests/search/test_answers.py`
- Rename `test_the_answer_eval_uses_the_coach_voice_and_a_new_prompt_version` to `test_the_answer_eval_keeps_the_version_2_instructions`. Assert `sha256(ANSWER_INSTRUCTIONS) == "5676230a…dad1b"`, `ANSWER_PROMPT_VERSION == 2`, that the prompt contains `"[Post title](URL)"`, and that `ANSWER_INSTRUCTIONS != mcp_server.ASK_INSTRUCTIONS`. Tagged `# spec: answer-evaluation / Generate answers (The live prompt changes)`.

`tests/search/test_answers_integration.py`
- Change the comment on `prompt.startswith("Question: …")` from "the MCP `ask` text itself" to "the eval's v2 instructions". No logic change.

## 6. Docs

- README "Ask for a full answer": "answer as a short write-up from a coach, like a blog post: … naming and quoting the posts it draws on, a few things to try this week, and a sources list at the end with the links." Add a sentence after `--install-command`: after this update, run it again.
- README answer-eval section: where it says the eval answers "the way `/ask-coach` does", add "with the version 2 instructions, which link posts inline so citations can be checked".
