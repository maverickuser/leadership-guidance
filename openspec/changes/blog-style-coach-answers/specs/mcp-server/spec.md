## MODIFIED Requirements

### Requirement: Local MCP server
The system SHALL provide a local MCP (Model Context Protocol) server named `leadership-coach`, started by `archive mcp`, that serves the archive's search to MCP clients on the same machine over standard input/output. It SHALL NOT listen on a network port. It SHALL work with any MCP-capable agent, including Claude Code and Claude Desktop. The server's instructions SHALL tell agents to answer advice questions from the archive as short write-ups from a coach in the first person to the reader, never claiming experience of its own, and quick lookups briefly; to credit the posts used by title in the text without linking them there; to end with a sources list that links each post used; and to mention the `ask` prompt.

#### Scenario: Connect from Claude Code
- **WHEN** the user registers the server in Claude Code as `leadership-coach`, as described in the README
- **THEN** Claude Code lists the archive's `search` tool and `ask` prompt and can use both

#### Scenario: Sources only at the end
- **WHEN** an MCP client reads the server's instructions
- **THEN** they tell the agent to name posts in the text without links and to put every link in a sources list at the end

### Requirement: Ask prompt
The server SHALL expose an MCP prompt named `ask` with one required argument, `question` (string). Getting the prompt SHALL return a single user message that contains the question and instructions for answering it from the archive with the `search` tool. The instructions SHALL tell the agent to:
- start a broad question with a summaries search to find the relevant posts, then search passages for evidence, preferring passages from those posts; a narrow factual question may go straight to passages; run more than one search when the question has several parts;
- write the answer as a short blog-style write-up from a coach: a title naming the topic, an opening that names the reader's problem and gives the direct answer, then a few short sections with `###` headings that flow from one to the next, one idea per section, in prose with no long bullet lists, then a "Try this week" section of two to four small actions; a narrow factual question needs only the title, the direct answer and the sources list;
- write as the coach in the first person ("I"), teaching the reader as "you" with practical guidance, rather than summarizing the posts one by one;
- never claim experiences, stories, jobs or results of the coach's own: experience and examples come from the posts and are credited to them;
- quote the posts' own words briefly, in quotation marks, attributed to the post they come from without inventing who the speaker is, and credit each point to its post by naming the post's title in the text, without a link;
- put no links anywhere in the answer except the final "Sources" section;
- say where posts disagree, stating what each post says, and say plainly when the archive does not cover part of the question, instead of filling the gap from general knowledge;
- finish with a "Sources" section, after "Try this week", listing each post used as a link with its title and URL, and its date.

Getting the prompt SHALL NOT search, call a language model or read the database; it SHALL only return text. A question that is empty or longer than 4,000 characters SHALL be rejected with an error the user can act on, and the rejection SHALL be logged without the question text.

#### Scenario: List prompts
- **WHEN** an MCP client lists the server's prompts
- **THEN** it sees `ask` with a description and its required `question` argument

#### Scenario: Ask in Claude Desktop
- **WHEN** the user picks the `ask` prompt in Claude Desktop and types "how do I give implicit feedback?"
- **THEN** the agent receives the whole question with the answering instructions, searches the archive, and answers as a short blog-style write-up from a coach, with a title, section headings, attributed quotes, posts named in the text, a "Try this week" list and a linked sources list

#### Scenario: Sources only at the end
- **WHEN** the agent answers from the `ask` prompt's instructions
- **THEN** the instructions it received tell it to name posts in the text without links and to put every link in the final "Sources" section

#### Scenario: The coach's voice
- **WHEN** the agent answers from the `ask` prompt's instructions
- **THEN** the instructions it received tell it to teach as the coach in the first person, address the reader as "you", and never claim experience of its own

#### Scenario: A narrow question
- **WHEN** the question is a narrow factual lookup
- **THEN** the instructions it received allow only the title, the direct answer and the sources list, with no "Try this week" list

#### Scenario: Empty question
- **WHEN** a client gets the `ask` prompt with an empty `question`
- **THEN** it receives an error saying a question is needed, and the server keeps running
