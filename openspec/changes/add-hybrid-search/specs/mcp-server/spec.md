## ADDED Requirements

### Requirement: Local MCP server
The system SHALL provide a local MCP (Model Context Protocol) server, started by an `archive` command, that serves the archive's search to MCP clients on the same machine over standard input/output. It SHALL NOT listen on a network port. It SHALL work with any MCP-capable agent, including Claude Code and Claude Desktop.

#### Scenario: Connect from Claude Code
- **WHEN** the user registers the server in Claude Code as described in the README
- **THEN** Claude Code lists the archive's `search` tool and can call it

### Requirement: Search tool
The server SHALL expose a tool named `search` with arguments `query` (string, required), `limit` (integer, optional, default 10, at most 30), `category` (list of strings, optional), `since` and `until` (ISO dates, optional), and `kind` (`all`, `passages` or `summaries`, optional, default `all`). It SHALL run hybrid search with re-ranking and return the results defined in `hybrid-search`, both as structured content and as readable text, together with the query, the filters applied and whether results were re-ranked. The tool description SHALL tell agents what the archive contains, its date range, the valid category names, that results are passages and post summaries to be collated into an answer, that summaries help find relevant posts while passages are the evidence to quote, and that broad questions can start with `kind: "summaries"`.

#### Scenario: Agent search
- **WHEN** an agent calls `search` with `query: "how to give feedback to a senior engineer"`
- **THEN** it receives up to 10 ranked passages, each with post title, URL, date, categories, section heading and text

#### Scenario: Filtered search
- **WHEN** an agent calls `search` with `query: "hiring"` and `since: "2025-01-01"`
- **THEN** every returned passage is from a post published on or after 2025-01-01

### Requirement: Data only
The server SHALL return archive data only. It SHALL NOT generate answers or summaries at search time and SHALL NOT call any language model; post summaries are generated at index time, stored, and returned marked as `kind: "summary"`. The calling agent writes the answer from the returned data.

#### Scenario: No generated text
- **WHEN** an agent calls `search`
- **THEN** every text field in the response comes from the store (archive passages or stored post summaries) or describes the search itself

### Requirement: Tool errors
Invalid arguments (empty query, unknown category, bad date, limit out of range), a missing store, a missing or rejected Gemini API key, and a Gemini API outage SHALL be returned as MCP tool errors with a message an agent can act on, and SHALL NOT crash the server. When semantic search is unavailable because of the API, the server SHALL return keyword-only results marked as such instead of failing.

#### Scenario: API outage
- **WHEN** the Gemini API is unreachable during a search
- **THEN** the tool returns keyword-only results with a note that semantic search was unavailable

#### Scenario: Bad argument
- **WHEN** an agent calls `search` with `limit: 500`
- **THEN** the tool returns an error saying the limit must be between 1 and 30

### Requirement: Fresh data and read-only access
The server SHALL open the store read-only and SHALL pick up a newer store produced by `archive index` (for example after the weekly update) without needing a restart. It SHALL NOT take the archive run lock, so indexing can run while the server is connected.

#### Scenario: Weekly update while connected
- **WHEN** `archive index` adds 2 new posts while an agent is connected
- **THEN** the next `search` call can return passages from those posts

### Requirement: Server logging
The server SHALL log to a file under `{output_dir}/logs/` and to standard error, never to standard output (which carries the protocol). Logs SHALL record tool calls with argument names, result counts, timings and errors, and MUST NOT record the API key or full passage text.

#### Scenario: Protocol stays clean
- **WHEN** the server handles a search with `--verbose`
- **THEN** nothing but protocol messages is written to standard output
