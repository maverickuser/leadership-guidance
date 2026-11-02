"""The MCP server through a real MCP client: in process, and once over stdio as `archive mcp`."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from mcp import Client

from newsletter_archive import mcp_server
from newsletter_archive.search import db
from newsletter_archive.search.indexer import Indexer, IndexOptions
from newsletter_archive.search.rerank import FakeReranker
from newsletter_archive.search.service import SearchResponse, SearchService

from .archive import small_config, write_archive
from .fakes import FakeEmbedder
from .test_indexer_integration import FakeSummarizer
from .test_retrieval_integration import FILLER, post

REPO = Path(__file__).resolve().parents[2]


def records():
    return [
        post("delegation", 1, [("Delegation basics", FILLER + " Delegate outcomes, not tasks.")], day=1),
        post("hiring", 2, [(None, FILLER + " Every loop has a bar raiser.")], category="Hiring", day=2),
        post("career", 3, [(None, FILLER + " Ask for a promotion with evidence.")], category="Career", day=6),
    ]


def index(cfg, recs):
    write_archive(cfg, recs)
    with db.connect(cfg, "writer") as conn:
        Indexer(cfg, conn, lambda *_: FakeEmbedder(dim=8), lambda: FakeSummarizer()).run(IndexOptions())


def make_service(cfg, embedder=None):
    @contextmanager
    def connect():
        with db.connect(cfg, "reader") as conn:
            yield conn

    def factory(model, dim):
        if isinstance(embedder, Exception):
            raise embedder
        return FakeEmbedder(model=model, dim=dim)

    return SearchService(cfg, connect, factory, FakeReranker())


def call(server, args):
    async def go():
        async with Client(server) as client:
            return await client.call_tool("search", args)

    return asyncio.run(go())


def tools(server):
    async def go():
        async with Client(server) as client:
            return (await client.list_tools()).tools

    return asyncio.run(go())


def text(result):
    return "\n".join(c.text for c in result.content)


# -- no database ---------------------------------------------------------------------------------


def test_description_without_an_index_is_generic():
    d = mcp_server.tool_description(None, default_limit=5)
    assert (
        "load shortly" in d and "default 5" in d and 'kind: "summaries"' in d and "Valid categories" not in d
    )


def test_render_text_with_no_results():
    resp = SearchResponse("zz", {"category": None, "since": None, "until": None, "kind": "all"}, False, False)
    resp.notes.append("semantic search skipped: down")
    out = mcp_server.render_text(resp)
    assert 'Search: "zz"' in out and "Note: semantic search skipped: down" in out and "No matching" in out


# -- with a store ------------------------------------------------------------------------------

db_only = pytest.mark.db


@pytest.fixture
def cfg(db_config):
    c = small_config(db_config)
    index(c, records())
    return c


@db_only
def test_tool_is_listed_with_a_description_from_the_index(cfg):
    svc = make_service(cfg)
    overview = svc.overview()
    assert (overview.posts, overview.first, overview.last) == (3, "2025-01-01", "2025-06-01")
    [tool] = tools(mcp_server.build_server(svc, overview)[0])
    assert tool.name == "search" and tool.annotations.read_only_hint is True
    assert "3 posts" in tool.description and "from 2025-01-01 to 2025-06-01" in tool.description
    assert "Valid categories: Career, Hiring, Leadership." in tool.description
    assert (
        "summaries to find the relevant posts" in tool.description and "evidence to quote" in tool.description
    )
    props = tool.input_schema["properties"]
    assert set(props) == {"query", "limit", "category", "since", "until", "kind"}
    assert tool.input_schema["required"] == ["query"]


@db_only
def test_search_returns_structured_and_readable_results(cfg):
    server = mcp_server.build_server(make_service(cfg), None)[0]
    result = call(server, {"query": '"bar raiser"', "limit": 3})
    assert not result.is_error
    data = result.structured_content
    assert data["query"] == '"bar raiser"' and data["semantic"] and data["reranked"]
    assert len(data["results"]) == 3 and data["results"][0]["post"]["id"] == "hiring"
    first = data["results"][0]
    assert {"rank", "score", "found_by", "kind", "id", "heading", "text", "post"} <= set(first)
    assert {"id", "title", "url", "published_date", "categories"} == set(first["post"])
    out = text(result)
    assert out.startswith('Search: ""bar raiser""')
    assert "[1] " in out and first["post"]["url"] in out and "re-ranked: yes" in out


@db_only
def test_filters_and_summaries(cfg):
    server = mcp_server.build_server(make_service(cfg), None)[0]
    data = call(server, {"query": "team", "since": "2025-02-01", "limit": 30}).structured_content
    assert {r["post"]["id"] for r in data["results"]} == {"hiring", "career"}
    data = call(server, {"query": "team", "kind": "summaries", "category": "hiring"}).structured_content
    assert [r["kind"] for r in data["results"]] == ["summary"] and data["filters"]["category"] == ["Hiring"]


@db_only
@pytest.mark.parametrize(
    "args, message",
    [
        ({"query": "x", "limit": 500}, "limit must be a whole number between 1 and 30"),
        ({"query": "   "}, "query must be a non-empty"),
        ({"query": "x", "category": ["Nope"]}, "valid categories: Career, Hiring, Leadership"),
        ({"query": "x", "since": "01/02/2025"}, "YYYY-MM-DD"),
    ],
)
def test_bad_arguments_are_tool_errors(cfg, args, message):
    result = call(mcp_server.build_server(make_service(cfg), None)[0], args)
    assert result.is_error and message in text(result)


@db_only
def test_schema_rejects_an_unknown_kind(cfg):
    result = call(mcp_server.build_server(make_service(cfg), None)[0], {"query": "x", "kind": "passage"})
    assert result.is_error


@db_only
def test_gemini_outage_gives_keyword_only_results(cfg):
    svc = make_service(cfg, embedder=RuntimeError("Gemini unavailable (HTTP 503)"))
    result = call(mcp_server.build_server(svc, None)[0], {"query": "bar raiser"})
    data = result.structured_content
    assert not result.is_error and not data["semantic"] and data["results"]
    assert "semantic search skipped: Gemini unavailable" in data["notes"][0] and "semantic: no" in text(
        result
    )


@db_only
def test_new_posts_appear_without_a_restart(cfg):
    server = mcp_server.build_server(make_service(cfg), None)[0]
    assert "fresh-post" not in json.dumps(call(server, {"query": "team", "limit": 30}).structured_content)
    index(cfg, [*records(), post("fresh-post", 9, [(None, FILLER + " A brand new weekly post.")], day=7)])
    data = call(server, {"query": "brand new weekly", "limit": 30}).structured_content
    assert "fresh-post" in {r["post"]["id"] for r in data["results"]}


@db_only
def test_no_index_is_a_tool_error(db_config):
    result = call(mcp_server.build_server(make_service(small_config(db_config)), None)[0], {"query": "x"})
    assert result.is_error and "run `archive index`" in text(result)


@db_only
def test_database_errors_are_tool_errors(cfg):
    import psycopg

    class Broken(SearchService):
        def search(self, request):
            raise psycopg.errors.InsufficientPrivilege("permission denied")

    svc = make_service(cfg)
    broken = Broken(cfg, svc.connect, svc.embedder_factory)
    result = call(mcp_server.build_server(broken, None)[0], {"query": "x"})
    assert result.is_error and "search database failed (InsufficientPrivilege)" in text(result)


@db_only
def test_logs_name_arguments_but_not_the_query(cfg, caplog):
    caplog.set_level("INFO", logger="newsletter_archive.mcp")
    call(mcp_server.build_server(make_service(cfg), None)[0], {"query": "secret question text", "limit": 2})
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "search args=['query', 'limit'] results=2" in logged and "secret question" not in logged


@db_only
@pytest.mark.slow
def test_archive_mcp_over_stdio_keeps_stdout_for_the_protocol(cfg, tmp_path, pg_admin):
    from mcp import StdioServerParameters

    s = cfg.search
    (tmp_path / "config.yaml").write_text(
        f"base_url: https://www.example.com\noutput_dir: {cfg.output_dir}\n"
        f"search:\n  embedding_model: {s.embedding_model}\n  embedding_dimension: {s.embedding_dimension}\n"
        f"  db_port: {s.db_port}\n  db_name: {s.db_name}\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        f"ARCHIVE_DB_READER_PASSWORD={cfg.db_reader_password.reveal()}\n"
        f"ARCHIVE_DB_WRITER_PASSWORD={cfg.db_writer_password.reveal()}\nGEMINI_API_KEY=\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ARCHIVE_DB_", "GEMINI"))}
    env["HF_HUB_OFFLINE"] = "1"  # never download the re-ranker in tests
    params = StdioServerParameters(
        command=str(Path(sys.executable).parent / "archive"),
        args=["mcp", "--verbose", "--config", str(tmp_path / "config.yaml")],
        env=env,
        cwd=str(tmp_path),
    )

    async def go():
        async with Client(params, read_timeout_seconds=60) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            return names, await client.call_tool("search", {"query": "bar raiser", "limit": 2})

    names, result = asyncio.run(go())
    assert names == ["search"] and not result.is_error  # any stray stdout would break the protocol
    data = result.structured_content
    assert not data["semantic"] and "GEMINI_API_KEY" in " ".join(data["notes"])
    logs = list((cfg.output_dir / "logs").glob("mcp-*.log"))
    assert logs and "search args=['query', 'limit']" in logs[0].read_text(encoding="utf-8")


def replace_db(cfg):
    from dataclasses import replace

    return replace(cfg, search=replace(cfg.search, db_name="no_such_database"))


# -- review fixes ------------------------------------------------------------------------------


@db_only
def test_description_fills_in_after_start_and_follows_the_index(cfg):
    now = [0.0]
    svc = make_service(cfg)
    server, live = mcp_server.build_server(svc)
    live.clock = lambda: now[0]
    [tool] = tools(server)
    assert "load shortly" in tool.description  # start-up never waited for the database
    live.start().join(5)
    [tool] = tools(server)
    assert (
        "3 posts" in tool.description and "Valid categories: Career, Hiring, Leadership." in tool.description
    )
    assert live.generation == 1
    index(cfg, [*records(), post("new-cat", 8, [(None, FILLER)], category="Strategy", day=8)])
    live.after_search(1)  # same version, checked recently: no refresh
    assert "4 posts" not in tools(server)[0].description
    now[0] += mcp_server.DESCRIPTION_REFRESH_SECONDS
    from newsletter_archive.search.keyword import clear_category_cache

    clear_category_cache()  # categories are cached for a minute on their own
    live.after_search(1)
    assert "4 posts" in tools(server)[0].description and "Strategy" in tools(server)[0].description


@db_only
def test_description_refresh_survives_a_missing_index(db_config, caplog):
    svc = make_service(small_config(db_config))
    _, live = mcp_server.build_server(svc)
    assert live.refresh() is False and live.checked_at is None
    assert "index not reachable for the tool description" in caplog.text
    live._lock.acquire()
    try:
        assert live.refresh() is False  # another refresh is already running
    finally:
        live._lock.release()


@db_only
def test_logged_argument_names_include_falsy_values(cfg, caplog):
    caplog.set_level("INFO", logger="newsletter_archive.mcp")
    call(mcp_server.build_server(make_service(cfg))[0], {"query": "x", "limit": 0, "category": []})
    assert "search args=['query', 'limit', 'category'] error=SearchError" in caplog.text


@db_only
def test_pooled_connections_explain_why_none_came(cfg):
    from dataclasses import replace

    from psycopg_pool import ConnectionPool

    from newsletter_archive.config import Secret
    from newsletter_archive.errors import DatabaseAuthError
    from newsletter_archive.search.service import SearchError

    info = db.conninfo(cfg, "reader")
    with ConnectionPool(info, min_size=1, max_size=1, kwargs={"autocommit": True}) as pool:
        connect = mcp_server.pooled_connect(pool, cfg, timeout=0.3)
        with connect() as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
            with pytest.raises(SearchError, match="all 1 search database connections are busy"), connect():
                pass
    wrong = replace(cfg, db_reader_password=Secret("not-the-password"))
    bad_info = info.replace(f"password={cfg.db_reader_password.reveal()}", "password=not-the-password")
    with ConnectionPool(bad_info, min_size=0, max_size=1, open=False) as pool:
        pool.open(wait=False)
        with pytest.raises(DatabaseAuthError), mcp_server.pooled_connect(pool, wrong, timeout=0.3)():
            pass


@db_only
def test_secrets_never_reach_responses_or_logs(cfg, caplog):
    """Even if an upstream error quoted a secret, the MCP response and the log stay clean."""
    from dataclasses import replace

    from newsletter_archive.config import Secret

    from ..conftest import TEST_GEMINI_KEY

    caplog.set_level("DEBUG")
    keyed = replace(cfg, gemini_api_key=Secret(TEST_GEMINI_KEY))
    leaky = RuntimeError(f"401 for key {TEST_GEMINI_KEY} and password {cfg.db_reader_password.reveal()}")
    svc = make_service(keyed, embedder=leaky)
    result = call(mcp_server.build_server(svc)[0], {"query": "bar raiser"})
    everything = json.dumps(result.structured_content) + text(result) + caplog.text
    assert "semantic search skipped" in everything and "[REDACTED]" in everything
    assert TEST_GEMINI_KEY not in everything and cfg.db_reader_password.reveal() not in everything
