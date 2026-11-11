## MODIFIED Requirements

### Requirement: Search tool
The server SHALL expose a tool named `search` with these arguments:
- `query`: string, required.
- `limit`: integer, optional, default 10, at most 30.
- `source`: list of source ids, optional.
- `category`: list of strings, optional.
- `since` and `until`: ISO dates, optional.
- `kind`: `all`, `passages` or `summaries`, optional, default `all`.

It SHALL run hybrid search with re-ranking and return the results defined in `hybrid-search`, both as structured content and as readable text, together with the query, the filters applied and whether results were re-ranked.

The tool description SHALL tell agents:
- what the archive contains: each source with its name, post count and date range;
- the valid category names per source;
- that results are passages and post summaries to be collated into an answer;
- that summaries help find relevant posts while passages are the evidence to quote;
- that broad questions can start with `kind: "summaries"`.

#### Scenario: Agent search
- **WHEN** an agent calls `search` with `query: "how to give feedback to a senior engineer"`
- **THEN** it receives up to 10 ranked passages, each with source, post title, URL, date, categories, section heading and text

#### Scenario: Filtered search
- **WHEN** an agent calls `search` with `query: "hiring"` and `since: "2025-01-01"`
- **THEN** every returned passage is from a post published on or after 2025-01-01

#### Scenario: Source search
- **WHEN** an agent calls `search` with `query: "on-call"` and `source: ["pragmatic-engineer"]`
- **THEN** every returned result is from The Pragmatic Engineer
