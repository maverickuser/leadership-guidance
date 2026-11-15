## Context

The `ask` prompt (`ASK_INSTRUCTIONS` in `mcp_server.py`) and `/ask-coach` share one block of instructions. The answer eval reuses that block as part of its answer prompt (`answers.answer_prompt`). Only the text changes; the code paths stay the same.

## Goals / Non-Goals

**Goals:** answers read as a lesson from a coach in the first person, with post titles linked in the text and a Sources list at the end. The answer eval keeps working.

**Non-Goals:** changing the research steps, the search tool, or the eval judge. Making the coach a persona with a name or a life story.

## Decisions (made with the user)

1. **The coach speaks as "I" to "you"** (approved sample, 2026-11-15).
2. **Titles are linked in the text, and Sources go at the bottom** (gate 1). This keeps the faithfulness judge and post coverage unchanged, so no judge version bump is needed.
3. **No invented experience.** "I" is the coach's teaching voice, not a source of facts. Without this rule, a first-person voice invites made-up stories ("When I ran a team of 40…"), which the faithfulness judge would rightly count as unsupported.

## Decisions (technical)

4. **One text block, three users.** `ASK_INSTRUCTIONS` stays the single source for the MCP prompt, the command file and the answer eval. `SERVER_INSTRUCTIONS` (the short hint every client sees) changes to match: "as a short lesson from a coach, in the first person to the reader". The prompt's description changes "teaching post" to "lesson from a coach".
5. **No low-level design document.** The change is a text edit plus tests, which the workflow counts as a trivial fix.
6. **Tests** check the key phrases in the prompt, the command file and the server instructions: first person, no experience of its own, Try this week, title links, Sources. They also check that the old "Never write as the author in the first person" line is gone.

## New instruction text (the "Write the answer" part; the research part is unchanged)

```
Write the answer as a short lesson from a coach who is teaching the reader:
- Start with a "## " title that names the topic. Then one paragraph that names the problem the
  reader faces and gives the direct answer.
- Then a few short sections with "### " headings, one idea each, a few sentences each. Mostly
  prose; a few short examples are fine, but no long bullet lists.
- Write as the coach, in the first person ("I"), teaching the reader as "you" with practical
  guidance, rather than summarizing the posts one by one.
- Never claim experiences, stories, jobs or results of your own. Experience and examples come
  from the posts; credit them ("as one post puts it").
- Quote the posts' own words briefly, in quotation marks, attributed to the post they come from;
  don't invent who the speaker is. Name each post you draw on by its title, as a link:
  [Post title](URL).
- Where posts disagree or show different sides, say what each post says and explain the
  difference.
- If the archive doesn't cover part of the question, say so plainly. Don't fill the gap from
  general knowledge.
- Close with "### Try this week": two to four small, concrete actions drawn from the lesson.
- End with a "Sources" list: each post used, as [Title](URL), with its date.
A narrow, factual question needs only the title, the direct answer and the Sources list.
```

## Risks / Trade-offs

- **A first-person voice could slip into made-up anecdotes** → decision 3 rules it out, and the faithfulness judge would catch such claims as unsupported.
- **Answers get a little longer** because of the Try this week list → it's kept to two to four lines. `ANSWER_OUTPUT_TOKENS` (4096) has room.
- **Eval comparisons across the voice change** → the new run gets a new label. Post coverage and faithfulness are compared with `posts-max-10x3` using `--compare`, and only with the user's yes on the cost (about $1.70).
