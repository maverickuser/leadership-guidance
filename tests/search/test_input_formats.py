"""Embedding input formats per model family (openspec use-local-embeddings, lld §3)."""

from newsletter_archive.search.embeddings import (
    GEMINI_FORMAT,
    LOCAL_FORMAT,
    input_format,
    passage_input,
    query_input,
    summary_input,
)


def test_dispatch_by_model_name():
    assert input_format("gemini-embedding-2") is GEMINI_FORMAT
    assert input_format("local:qwen3-embedding-0.6b-archive-v5") is LOCAL_FORMAT


def test_gemini_format_unchanged():
    assert GEMINI_FORMAT.version == 1
    assert (
        GEMINI_FORMAT.passage("T", "H", "body")
        == "title: T | text: H\nbody"
        == passage_input("T", "H", "body")
    )
    assert GEMINI_FORMAT.passage("T", None, "body") == "title: T | text: body"
    assert GEMINI_FORMAT.summary("T", "S") == "title: T | text: Summary\nS" == summary_input("T", "S")
    assert GEMINI_FORMAT.query("q") == "task: search result | query: q" == query_input("q")


def test_local_format_matches_what_the_model_was_trained_on():
    assert LOCAL_FORMAT.version == "local-1"
    assert LOCAL_FORMAT.passage("T", "H", "body") == "T\nH\nbody"
    assert LOCAL_FORMAT.passage("T", None, "body") == "T\nbody"
    assert LOCAL_FORMAT.passage("T", "", "body") == "T\nbody"
    assert LOCAL_FORMAT.summary("T", "S") == "T\nS"
    assert LOCAL_FORMAT.query("how do I delegate?") == (
        "Instruct: Given a question, find passages from a leadership newsletter that answer it\n"
        "Query:how do I delegate?"
    )
