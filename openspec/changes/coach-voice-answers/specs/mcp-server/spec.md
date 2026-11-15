## MODIFIED Requirements

### Requirement: Ask prompt
The server SHALL expose an MCP prompt named `ask` with one required argument, `question` (string). Getting the prompt SHALL return a single user message that contains the question and instructions for answering it from the archive with the `search` tool. The instructions SHALL tell the agent to:
- start a broad question with a summaries search to find the relevant posts, then search passages for evidence, preferring passages from those posts; a narrow factual question may go straight to passages; run more than one search when the question has several parts;
- write the answer as a short lesson from a coach: a title naming the topic, an opening that names the reader's problem and gives the direct answer, then short sections with `###` headings, one idea per section, mostly prose with no long bullet lists, then a "Try this week" section of two to four small actions, each linked to the post it comes from; a narrow factual question needs only the title, the direct answer and the sources list;
- write as the coach in the first person ("I"), teaching the reader as "you" with practical guidance, rather than summarizing the posts one by one;
- never claim experiences, stories, jobs or results of the coach's own: experience and examples come from the posts and are credited to them;
- quote the posts' own words briefly, in quotation marks, attributed to the post they come from without inventing who the speaker is, and each time it uses a point from a post, name that post by its title in the text as a link to its URL;
- say where posts disagree, stating what each post says, and say plainly when the archive does not cover part of the question, instead of filling the gap from general knowledge;
- finish with a "Sources" section, after "Try this week", listing the posts used, each with its date.

Getting the prompt SHALL NOT search, call a language model or read the database; it SHALL only return text. A question that is empty or longer than 4,000 characters SHALL be rejected with an error the user can act on, and the rejection SHALL be logged without the question text.

#### Scenario: List prompts
- **WHEN** an MCP client lists the server's prompts
- **THEN** it sees `ask` with a description and its required `question` argument

#### Scenario: Ask in Claude Desktop
- **WHEN** the user picks the `ask` prompt in Claude Desktop and types "how do I give implicit feedback?"
- **THEN** the agent receives the whole question with the answering instructions, searches the archive, and answers as a short lesson from a coach, with a title, section headings, attributed quotes, linked post titles, a "Try this week" list and a sources list

#### Scenario: The coach's voice
- **WHEN** the agent answers from the `ask` prompt's instructions
- **THEN** the instructions it received tell it to teach as the coach in the first person, address the reader as "you", and never claim experience of its own

#### Scenario: A narrow question
- **WHEN** the question is a narrow factual lookup
- **THEN** the instructions it received allow only the title, the direct answer and the sources list, with no "Try this week" list

#### Scenario: Empty question
- **WHEN** a client gets the `ask` prompt with an empty `question`
- **THEN** it receives an error saying a question is needed, and the server keeps running
