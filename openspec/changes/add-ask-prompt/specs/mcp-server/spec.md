## ADDED Requirements

### Requirement: Ask prompt
The server SHALL expose an MCP prompt named `ask` with one required argument, `question` (string). Getting the prompt SHALL return a single user message that contains the question and instructions for answering it from the archive with the `search` tool. The instructions SHALL tell the agent to:
- search post summaries first to find the relevant posts, then passages of the most relevant posts for evidence, and run more than one search when the question has several parts;
- write the answer as 3 to 6 paragraphs of prose, opening with the direct answer, one main point per paragraph;
- link the post title to its URL inline after each claim drawn from a post, quote the author's own words where they add something, and end with a list of the sources used;
- say where posts disagree, and say plainly when the archive does not cover part of the question, instead of filling the gap from general knowledge.

Getting the prompt SHALL NOT search, call a language model or read the database; it SHALL only return text. A question that is empty or longer than the search query limit SHALL be rejected with an error the user can act on.

#### Scenario: List prompts
- **WHEN** an MCP client lists the server's prompts
- **THEN** it sees `ask` with a description and its required `question` argument

#### Scenario: Ask a question
- **WHEN** the user runs `/mcp__newsletter-archive__ask how do I give implicit feedback?` in Claude Code
- **THEN** the agent receives the question with the answering instructions, searches the archive, and answers in paragraphs with inline links to the posts it used and a sources list

#### Scenario: Empty question
- **WHEN** a client gets the `ask` prompt with an empty `question`
- **THEN** it receives an error saying a question is needed, and the server keeps running

## MODIFIED Requirements

### Requirement: Local MCP server
The system SHALL provide a local MCP (Model Context Protocol) server, started by an `archive` command, that serves the archive's search to MCP clients on the same machine over standard input/output. It SHALL NOT listen on a network port. It SHALL work with any MCP-capable agent, including Claude Code and Claude Desktop. The server's instructions SHALL tell agents to answer archive questions in paragraphs with inline links to the posts used, and mention the `ask` prompt.

#### Scenario: Connect from Claude Code
- **WHEN** the user registers the server in Claude Code as described in the README
- **THEN** Claude Code lists the archive's `search` tool and `ask` prompt and can use both
