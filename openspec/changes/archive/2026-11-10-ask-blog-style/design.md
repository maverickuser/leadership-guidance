## Context

The `ask` prompt and the `/ask-coach` command share one block of text, `ASK_INSTRUCTIONS` in
`src/newsletter_archive/mcp_server.py`. The server also sends a one-paragraph
`SERVER_INSTRUCTIONS` to every connected agent. Both currently ask for "paragraphs" of prose
that report on the posts. The server returns data only; the calling agent writes the answer, so
the style is controlled entirely by this instruction text.

## Goals / Non-Goals

**Goals:**
- Answers read like a short teaching blog post written to the reader.
- Keep everything that makes answers trustworthy: inline links, short real quotes, saying where
  posts disagree, saying what the archive doesn't cover, a dated Sources list.
- Keep the research steps unchanged.

**Non-Goals:**
- No answer generation on the server, no model calls, no new arguments (e.g. a style switch).
- No change to search, ranking, the database or the index.

## Decisions

1. **Change the instruction text only.** Rewrite the "Write the answer" part of
   `ASK_INSTRUCTIONS` and the answering sentence in `SERVER_INSTRUCTIONS`. The `/ask-coach` file
   already embeds `ASK_INSTRUCTIONS`, so it follows with no code change. Alternative considered:
   a `style` argument on the prompt. Rejected: Claude Code passes only one word to MCP prompt
   arguments, and the user wants one style everywhere.
2. **Shape of the answer:** a `##` title, an opening paragraph with the direct answer, then
   `###` sections with one point each, mostly prose. A few short examples may be listed; no long
   bullet lists. This matches the sample answer the user approved.
3. **Voice:** second person ("you"), practical. Explicitly forbid writing as the author in the
   first person, since the user rejected that version.
4. **Quotes:** brief, in quotation marks, attributed in plain words (for example "as one
   engineering leader puts it"), with the post linked inline. No author name is required, which
   keeps answers neutral and avoids impersonation.
5. **Gaps:** keep the rule to say plainly what the archive doesn't cover; in a blog shape this
   can be a closing sentence or short section.
6. **Updating installed commands:** no migration. The README tells users to re-run
   `archive mcp --install-command`, which already replaces a file it wrote before.

### Decisions (made with the user)

- Teaching blog style, written to "you", not third-person reporting and not first person as the
  author (2026-11-10, after comparing sample answers).
- One style for all answers; no option to switch.

## Risks / Trade-offs

- [Short questions get over-structured answers] → instructions say "short": a few sections, a few
  sentences each. Accepted; the user wants one consistent style.
- [Already-installed `/ask-coach` files keep the old text] → README note to re-run
  `--install-command`.
- [Tests that match exact phrases break] → update them to assert the new key phrases.
