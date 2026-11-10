## MODIFIED Requirements

### Requirement: Local MCP server
The system SHALL provide a local MCP (Model Context Protocol) server named `leadership-coach`, started by `archive mcp`, that serves the archive's search to MCP clients on the same machine over standard input/output. It SHALL NOT listen on a network port. It SHALL work with any MCP-capable agent, including Claude Code and Claude Desktop. The server's instructions SHALL tell agents to answer advice questions from the archive as short teaching posts written to the reader, and quick lookups briefly, with inline links to the posts used, and mention the `ask` prompt.

#### Scenario: Connect from Claude Code
- **WHEN** the user registers the server in Claude Code as `leadership-coach`, as described in the README
- **THEN** Claude Code lists the archive's `search` tool and `ask` prompt and can use both

### Requirement: Ask prompt
The server SHALL expose an MCP prompt named `ask` with one required argument, `question` (string). Getting the prompt SHALL return a single user message that contains the question and instructions for answering it from the archive with the `search` tool. The instructions SHALL tell the agent to:
- start a broad question with a summaries search to find the relevant posts, then search passages for evidence, preferring passages from those posts; a narrow factual question may go straight to passages; run more than one search when the question has several parts;
- write the answer as a short teaching blog post: a title naming the topic, an opening paragraph with the direct answer, then short sections with `###` headings, one main point per section, mostly prose with no long bullet lists; a narrow factual question needs only the title, the direct answer and the sources list;
- address the reader as "you" with practical guidance, rather than summarizing the posts one by one, and never write as the author in the first person;
- quote the posts' own words briefly, in quotation marks, attributed to the post they come from without inventing who the speaker is, and link the post title to its URL inline after each claim drawn from a post;
- say where posts disagree, stating what each post says, and say plainly when the archive does not cover part of the question, instead of filling the gap from general knowledge;
- end with a "Sources" list of the posts used, each with its date.

Getting the prompt SHALL NOT search, call a language model or read the database; it SHALL only return text. A question that is empty or longer than 4,000 characters SHALL be rejected with an error the user can act on, and the rejection SHALL be logged without the question text.

#### Scenario: List prompts
- **WHEN** an MCP client lists the server's prompts
- **THEN** it sees `ask` with a description and its required `question` argument

#### Scenario: Ask in Claude Desktop
- **WHEN** the user picks the `ask` prompt in Claude Desktop and types "how do I give implicit feedback?"
- **THEN** the agent receives the whole question with the answering instructions, searches the archive, and answers as a short teaching post written to the reader, with a title, section headings, attributed quotes, inline links to the posts it used and a sources list

#### Scenario: Not the author's voice
- **WHEN** the agent answers from the `ask` prompt's instructions
- **THEN** the instructions it received tell it to address the reader as "you" and not to write as the author in the first person

#### Scenario: Empty question
- **WHEN** a client gets the `ask` prompt with an empty `question`
- **THEN** it receives an error saying a question is needed, and the server keeps running

### Requirement: Claude Code ask command
Because Claude Code passes only the first word of the text after an MCP prompt command to a one-argument prompt, the system SHALL provide `archive mcp --install-command`, which writes a Claude Code command file `ask-coach.md` into the user's Claude Code commands folder (`~/.claude/commands`, or `$CLAUDE_CONFIG_DIR/commands` when that is set). The file SHALL pass the user's whole question (`$ARGUMENTS`) with the same answering instructions as the `ask` prompt. Running the option again SHALL update a file it wrote before. It SHALL NOT overwrite a different file of the same name unless `--force` is given. It SHALL NOT need the database, the config file or any secret.

#### Scenario: Install and ask
- **WHEN** the user runs `archive mcp --install-command`, then `/ask-coach how do I give implicit feedback?` in Claude Code
- **THEN** Claude receives the whole question with the answering instructions and answers from the archive as a short teaching post with sources

#### Scenario: Update after upgrading
- **WHEN** an `ask-coach.md` written by an older version exists and the user runs `archive mcp --install-command` again
- **THEN** the file is replaced with the current answering instructions

#### Scenario: Someone else's file
- **WHEN** `~/.claude/commands/ask-coach.md` exists and was not written by `archive`
- **THEN** the command leaves it unchanged, says so, and suggests `--force`
