# Low-level design: ask-blog-style

Text-only change in one module. No schema, config, dependency, secret or live-call impact.

## 1. Architecture

Unchanged. `ASK_INSTRUCTIONS` is shared by the `ask` prompt (`ask_message`) and the `/ask-coach`
file (`command_text`). Changing the constant changes both.

## 2. Code changes (`src/newsletter_archive/mcp_server.py`)

### 2.1 `SERVER_INSTRUCTIONS`

```
Search a personal newsletter archive with the `search` tool. Answer as a short teaching post
written to the reader ("you"), link each post you draw on by title and URL, and end with the
sources used. For a full researched answer, use the `ask` prompt (in Claude Code:
`/ask-coach <question>`).
```

### 2.2 `ASK_INSTRUCTIONS`, "Write the answer" block

The opening line and "Research" block stay word for word. The writing block becomes:

```
Write the answer as a short teaching blog post for the reader:
- Start with a "## " title that names the topic, then one paragraph with the direct answer.
- Then a few short sections with "### " headings, one main point each, a few sentences each.
  Mostly prose; a few short examples are fine, but no long bullet lists.
- Write to the reader as "you", with practical guidance. Don't report what "the author" or
  "the post" says, and never write as the author in the first person.
- Quote the posts' own words briefly, in quotation marks, attributed in plain words (for example
  "as one engineering leader puts it"). After each claim drawn from a post, link the post:
  [Post title](URL).
- Where posts disagree or show different sides, say so and explain the difference.
- If the archive doesn't cover part of the question, say so plainly. Don't fill the gap from
  general knowledge.
- End with a "Sources" list: each post used, as [Title](URL), with its date.
```

### 2.3 Descriptions

- `ask` prompt description: `Answer a question from the archive as a short teaching post, with
  links to the posts used.`
- `command_text` front-matter description: `Answer from the leadership-coach archive as a
  teaching post with sources`.

## 3. Data flow, errors, degraded modes

Unchanged: empty or over-4,000-character questions are still rejected; logging still records
only the length.

## 4. Secrets and safety

None touched. `scripts/check_safety.py` unaffected.

## 5. Testing (`tests/search/test_mcp_server.py`)

- `test_ask_prompt_is_listed`: assert "teaching post" in the description (was "paragraphs").
- `test_ask_prompt_returns_the_question_and_how_to_answer`: replace `"3 to 6 paragraphs"` with
  `'"## " title'`, `'"### " headings'`, `'as "you"'`, `"never write as the author in the first
  person"`, `"in quotation marks"`; keep the rest. Also assert `"3 to 6 paragraphs"` is absent.
- `test_server_name_and_instructions`: `"teaching post"` instead of `"paragraphs"`.
- Command tests: add/adjust an assertion that `command_text()` contains the new instructions,
  and that re-running install over an old marker file replaces it with the new text (covers the
  "Update after upgrading" scenario, if not already covered).
- No db or integration tests needed: nothing touches Postgres.

## 6. Docs

README line 205: "answer in 3–6 paragraphs" becomes "answer as a short teaching post written to
you, with a title and short sections, quoting and linking each post it draws on, and ending with
a sources list". The existing line about re-running `--install-command` stays.
