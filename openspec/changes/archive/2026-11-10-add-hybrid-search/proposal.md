## Why

The archive holds every post as clean JSON (252 posts, about 730k words, roughly 1M tokens), which is too large to hand to an AI agent in one go. The JSON files are the raw data source; they need to be persisted in a searchable form so an agent can ask a natural-language question and get back all the matching data, which it then turns into a refined answer. Keyword search (BM25) finds exact terms and names; semantic search finds passages with the same meaning in different words. Results must combine both, then be re-ranked so the most relevant data comes first.

## What Changes

- **Searchable store built from the JSON**: each post is split into passages along its sections; every passage is stored with its post's metadata, indexed for BM25 keyword search, and embedded for semantic search with Google's `gemini-embedding-2` model. The storage technology is decided in the low-level design.
- **Post summaries**: one short summary per post, generated once with a Gemini text model on the same key, stored and searched beside the passages so broad questions ("themes across all posts") find the right posts. Approach chosen after a research review (`retrieval-options.md`); knowledge graphs deferred.
- **Incremental indexing**: a new `archive index` command builds and updates the store; only new or changed posts are processed, and removed posts are dropped. `archive run` (and so the weekly scheduled job) ends with an index step.
- **Hybrid search with re-ranking**: each query runs BM25 and semantic search, combines both result lists, and re-ranks the combined candidates with a re-ranking model. The re-ranker is chosen in the low-level design.
- **Local MCP server**: search is exposed through a local MCP (Model Context Protocol) server that any MCP-capable agent can connect to (Claude Code, Claude Desktop and others). A `search` tool takes a text query (with optional filters) and returns the matching passages with their post metadata. The calling agent collates them and writes the answer; the server does not generate answers.
- **MCP is the only search interface**: no search command in the CLI and no public Python API for search.
- **Gemini API key**: `GEMINI_API_KEY` lives only in `.env` and, like the cookie, is never printed, logged or committed.
- **Search-quality evaluation**: a script measures retrieval quality on the user's own test questions, so ranking and model changes can be compared.

Out of scope: answer generation inside the server, a web UI, structured or statistical queries over post metadata (counts, lists), multi-turn chat.

## Capabilities

### New Capabilities

- `search-indexing`: passages from the JSON posts, stored metadata, BM25 index, Gemini embeddings, incremental updates, safe updates.
- `hybrid-search`: BM25 and semantic retrieval, combining results, re-ranking, filters, result content, quality evaluation.
- `mcp-server`: the local MCP server, its `search` tool contract, connection by agents, error handling.

### Modified Capabilities

- `archive-cli`: new `index` command and a command to start the MCP server; `run` also indexes; config gains search settings; secret handling covers the Gemini API key.

## Impact

- New code for indexing, search and the MCP server; new dependencies for the Gemini API client, the MCP server SDK and the re-ranker (chosen in the low-level design).
- New external service: the Gemini API. Post passages are sent once to build embeddings (about 1M tokens for the current archive, then only new posts), and each search query is sent at search time. Paid-tier terms should be used so Google does not use the content to improve its products.
- New local data in `{output_dir}` (git-ignored, outside the repo) for the store.
- The project rule "no cloud services" gains one explicit exception: the Gemini API for embeddings and post summaries (and the re-ranker, if the chosen one is an API).
- README, CI and the safety check cover the new commands, the MCP setup and the API key.
