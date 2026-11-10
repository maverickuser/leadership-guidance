## ADDED Requirements

### Requirement: Ask prompt
The server SHALL expose an MCP prompt named `ask` with one required argument, `question` (string). Getting the prompt SHALL return a single user message that contains the question and instructions for answering it from the archive with the `search` tool. The instructions SHALL tell the agent to:
- start a broad question with a summaries search to find the relevant posts, then search passages for evidence, preferring passages from those posts; a narrow factual question may go straight to passages; run more than one search when the question has several parts;
- write the answer as 3 to 6 paragraphs of prose, opening with the direct answer, one main point per paragraph;
- link the post title to its URL inline after each claim drawn from a post, quote the author's own words where they add something, and end with a list of the sources used;
- say where posts disagree, and say plainly when the archive does not cover part of the question, instead of filling the gap from general knowledge.

Getting the prompt SHALL NOT search, call a language model or read the database; it SHALL only return text. A question that is empty or longer than 4,000 characters SHALL be rejected with an error the user can act on, and the rejection SHALL be logged without the question text.

#### Scenario: List prompts
- **WHEN** an MCP client lists the server's prompts
- **THEN** it sees `ask` with a description and its required `question` argument

#### Scenario: Ask in Claude Desktop
- **WHEN** the user picks the `ask` prompt in Claude Desktop and types "how do I give implicit feedback?"
- **THEN** the agent receives the whole question with the answering instructions, searches the archive, and answers in paragraphs with inline links to the posts it used and a sources list

#### Scenario: Empty question
- **WHEN** a client gets the `ask` prompt with an empty `question`
- **THEN** it receives an error saying a question is needed, and the server keeps running

### Requirement: Claude Code ask command
Because Claude Code passes only the first word of the text after an MCP prompt command to a one-argument prompt, the system SHALL provide `archive mcp --install-command`, which writes a Claude Code command file `ask-coach.md` into the user's Claude Code commands folder (`~/.claude/commands`, or `$CLAUDE_CONFIG_DIR/commands` when that is set). The file SHALL pass the user's whole question (`$ARGUMENTS`) with the same answering instructions as the `ask` prompt. Running the option again SHALL update a file it wrote before. It SHALL NOT overwrite a different file of the same name unless `--force` is given. It SHALL NOT need the database, the config file or any secret.

#### Scenario: Install and ask
- **WHEN** the user runs `archive mcp --install-command`, then `/ask-coach how do I give implicit feedback?` in Claude Code
- **THEN** Claude receives the whole question with the answering instructions and answers from the archive in paragraphs with sources

#### Scenario: Someone else's file
- **WHEN** `~/.claude/commands/ask-coach.md` exists and was not written by `archive`
- **THEN** the command leaves it unchanged, says so, and suggests `--force`

## MODIFIED Requirements

### Requirement: Local MCP server
The system SHALL provide a local MCP (Model Context Protocol) server named `leadership-coach`, started by `archive mcp`, that serves the archive's search to MCP clients on the same machine over standard input/output. It SHALL NOT listen on a network port. It SHALL work with any MCP-capable agent, including Claude Code and Claude Desktop. The server's instructions SHALL tell agents to answer archive questions in paragraphs with inline links to the posts used, and mention the `ask` prompt.

#### Scenario: Connect from Claude Code
- **WHEN** the user registers the server in Claude Code as `leadership-coach`, as described in the README
- **THEN** Claude Code lists the archive's `search` tool and `ask` prompt and can use both

### Requirement: Data only
The server SHALL return archive data only. It SHALL NOT generate answers or summaries at search time and SHALL NOT call any language model; post summaries are generated at index time, stored, and returned marked as `kind: "summary"`. The calling agent writes the answer from the returned data. The `ask` prompt is the one exception to "archive data only": it returns fixed instruction text plus the user's own question, and still calls no model.

#### Scenario: No generated text
- **WHEN** an agent calls `search`
- **THEN** every text field in the response comes from the store (archive passages or stored post summaries) or describes the search itself
