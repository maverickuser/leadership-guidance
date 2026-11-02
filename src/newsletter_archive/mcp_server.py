"""Local MCP server (``archive mcp``): the archive's search as one tool, over stdio only.

The server returns archive data and nothing else: passages and the post summaries stored at
index time. It never calls a language model; the calling agent writes the answer. It reads
the index through a small read-only connection pool and picks up a new index version
(the weekly run, a swap) on the next search, without a restart and without the run lock.

Logs go to ``{output_dir}/logs/mcp-*.log`` and stderr, never stdout (stdout carries the
protocol). They record argument names, result counts, timings and errors, never the query
text, passage text or any key.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated, Literal

import psycopg
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__
from .config import Config
from .errors import ArchiveError
from .search.service import ArchiveOverview, SearchError, SearchRequest, SearchResponse, SearchService

log = logging.getLogger("newsletter_archive.mcp")

SERVER_NAME = "newsletter-archive"
POOL_MAX = 4
POOL_TIMEOUT_SECONDS = 5.0


def tool_description(overview: ArchiveOverview | None, max_limit: int = 30) -> str:
    """What the archive holds and how to search it well (built from the live index)."""
    if overview is None:
        holding = "A personal archive of a leadership newsletter (the index wasn't reachable at start-up)."
        cats = ""
    else:
        span = f"from {overview.first} to {overview.last}" if overview.first else "with no dates"
        holding = f"A personal archive of {overview.posts} posts from a leadership newsletter, {span}."
        cats = f"\nValid categories: {', '.join(overview.categories)}." if overview.categories else ""
    return (
        f"Search the newsletter archive. {holding}{cats}\n\n"
        "Hybrid search (keywords + meaning, then re-ranked) over passages of the posts and one "
        "stored summary per post. Results are archive data to collate into your answer:\n"
        '- kind "summary": what a whole post is about. Use summaries to find the relevant posts.\n'
        '- kind "passage": the post\'s own words. Passages are the evidence to quote.\n'
        'For broad questions ("what does the newsletter say about hiring?") start with '
        'kind: "summaries", then search passages of the most relevant posts. For specific '
        "questions search passages directly. Put exact phrases in double quotes. Always cite the "
        "post title and URL.\n"
        f"Arguments: query (required), limit (1-{max_limit}, default 10), category (one or more "
        "category names), since / until (YYYY-MM-DD, inclusive), kind (all | passages | summaries)."
    )


def render_text(resp: SearchResponse) -> str:
    """A readable version of the response (the structured content carries the same data)."""
    filters = ", ".join(f"{k}={v}" for k, v in resp.filters.items() if v not in (None, "all"))
    head = [
        f'Search: "{resp.query}"' + (f" ({filters})" if filters else ""),
        f"semantic: {'yes' if resp.semantic else 'no'}; re-ranked: {'yes' if resp.reranked else 'no'}; "
        f"results: {len(resp.results)}",
    ]
    head += [f"Note: {n}" for n in resp.notes]
    if not resp.results:
        return "\n".join([*head, "No matching passages or summaries."])
    blocks = []
    for r in resp.results:
        where = f"{r.post.title} — {r.heading}" if r.heading else r.post.title
        meta = "; ".join(x for x in (r.post.published_date or "", ", ".join(r.post.categories)) if x)
        blocks.append(
            f"[{r.rank}] {r.kind} · {where} ({meta}) · score {r.score:g} · found by {r.found_by}\n"
            f"{r.post.url}\n{r.text}"
        )
    return "\n".join(head) + "\n\n" + "\n\n".join(blocks)


def build_server(service: SearchService, overview: ArchiveOverview | None) -> MCPServer:
    s = service.s
    server = MCPServer(
        SERVER_NAME,
        version=__version__,
        instructions="Search a personal newsletter archive with the `search` tool; cite post URLs.",
        warn_on_duplicate_tools=False,
    )

    def search(
        query: Annotated[str, Field(description="The question or keywords; quote exact phrases.")],
        limit: Annotated[
            int | None, Field(description="Number of results (default 10; maximum in the tool description).")
        ] = None,
        category: Annotated[
            list[str] | str | None, Field(description="Category name(s), case-insensitive.")
        ] = None,
        since: Annotated[str | None, Field(description="Earliest publication date, YYYY-MM-DD.")] = None,
        until: Annotated[str | None, Field(description="Latest publication date, YYYY-MM-DD.")] = None,
        kind: Annotated[
            Literal["all", "passages", "summaries"], Field(description="Passages, summaries or both.")
        ] = "all",
    ) -> CallToolResult:
        given = [
            n for n, v in (("limit", limit), ("category", category), ("since", since), ("until", until)) if v
        ]
        args = ["query", *given] + (["kind"] if kind != "all" else [])
        started = time.monotonic()
        try:
            request = SearchRequest.create(
                query,
                limit,
                category,
                since,
                until,
                kind,
                default_limit=s.default_limit,
                max_limit=s.max_limit,
            )
            resp = service.search(request)
        except (SearchError, ArchiveError, psycopg.Error) as e:
            message = str(e) if isinstance(e, ArchiveError) else _db_message(e)
            log.info("search args=%s error=%s (%.1fs)", args, type(e).__name__, time.monotonic() - started)
            raise ToolError(message) from None
        log.info(
            "search args=%s results=%d semantic=%s reranked=%s notes=%d (%.1fs)",
            args,
            len(resp.results),
            resp.semantic,
            resp.reranked,
            len(resp.notes),
            time.monotonic() - started,
        )
        return CallToolResult(
            content=[TextContent(type="text", text=render_text(resp))], structured_content=resp.to_dict()
        )

    server.add_tool(
        search,
        name="search",
        title="Search the newsletter archive",
        description=tool_description(overview, s.max_limit),
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    )
    return server


def _db_message(e: BaseException) -> str:
    return f"the search database failed ({type(e).__name__}); check it with `docker compose ps`"


def make_service(config: Config) -> tuple[SearchService, Callable[[], None]]:  # pragma: no cover - real deps
    """The real service: read-only pool, Gemini query embedder, Qwen3 re-ranker loading in the
    background. Returns (service, close)."""
    from psycopg_pool import ConnectionPool

    from .search import db
    from .search.rerank import Reranking, qwen_loader
    from .search.service import gemini_query_embedder

    s = config.search
    pool = ConnectionPool(
        db.conninfo(config, "reader"),
        min_size=1,
        max_size=POOL_MAX,
        kwargs={"autocommit": True},
        check=ConnectionPool.check_connection,  # survives a database restart
        open=False,
        name="archive-reader",
    )
    pool.open(wait=False)  # never blocks start-up; a stopped database fails each search instead
    reranking = Reranking(qwen_loader(s.reranker_model, config.output_dir), s.rerank_timeout_seconds)

    def connect() -> AbstractContextManager[psycopg.Connection]:
        return pool.connection(timeout=POOL_TIMEOUT_SECONDS)

    service = SearchService(config, connect, gemini_query_embedder(config), reranking)

    def close() -> None:
        reranking.close()
        pool.close()

    return service, close


def serve(config: Config, verbose: bool = False) -> None:  # pragma: no cover - runs the stdio loop
    from .runlog import close_logging, setup_logging

    path = setup_logging(config, verbose, stream=sys.stderr, prefix="mcp")
    log.info("MCP server %s starting (log: %s)", __version__, path)
    if not config.gemini_api_key:
        log.warning("GEMINI_API_KEY is not set: searches will use keywords only")
    service, close = make_service(config)
    overview = _overview(service)
    try:
        build_server(service, overview).run("stdio")
    finally:
        close()
        log.info("MCP server stopped")
        close_logging()


def _overview(service: SearchService) -> ArchiveOverview | None:
    try:
        overview = service.overview()
    except (ArchiveError, psycopg.Error) as e:
        log.warning("index not reachable at start-up (%s); the tool description is generic", e)
        return None
    log.info(
        "index generation %s: %d posts, %d categories",
        overview.generation.generation,
        overview.posts,
        len(overview.categories),
    )
    return overview


__all__ = ["build_server", "render_text", "serve", "tool_description"]
