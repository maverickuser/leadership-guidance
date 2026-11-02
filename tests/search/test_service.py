"""Search request validation (no database)."""

from __future__ import annotations

from datetime import date

import pytest

from newsletter_archive.search.service import SearchError, SearchRequest


def test_defaults_and_normalising():
    r = SearchRequest.create("  how   to delegate ")
    assert (r.query, r.limit, r.categories, r.since, r.until, r.kind) == (
        "how to delegate",
        10,
        (),
        None,
        None,
        "all",
    )
    r = SearchRequest.create("q", 5, "Hiring", "2025-01-01", date(2025, 2, 1), "summaries")
    assert r.categories == ("Hiring",) and r.since == date(2025, 1, 1) and r.until == date(2025, 2, 1)
    assert SearchRequest.create("q", category=["A", " ", "B"], kind=None).categories == ("A", "B")
    assert SearchRequest.create("q", since="").since is None


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"query": ""}, "non-empty"),
        ({"query": "   "}, "non-empty"),
        ({"query": 5}, "non-empty"),
        ({"query": "x" * 1001}, "limit is 1000"),
        ({"query": "q", "limit": 0}, "between 1 and 30"),
        ({"query": "q", "limit": 31}, "between 1 and 30"),
        ({"query": "q", "limit": True}, "between 1 and 30"),
        ({"query": "q", "limit": "5"}, "between 1 and 30"),
        ({"query": "q", "category": 3}, "category must be"),
        ({"query": "q", "category": ["A", 1]}, "category must be"),
        ({"query": "q", "since": "31/03/2025"}, "YYYY-MM-DD"),
        ({"query": "q", "until": 20250101}, "YYYY-MM-DD"),
        ({"query": "q", "since": "2025-02-01", "until": "2025-01-01"}, "on or before"),
        ({"query": "q", "kind": "passage"}, "all, passages, summaries"),
    ],
)
def test_invalid_requests_say_what_is_allowed(kwargs, message):
    with pytest.raises(SearchError, match=message):
        SearchRequest.create(**kwargs)


def test_kind_must_be_a_string_and_datetimes_become_dates():
    from datetime import datetime

    with pytest.raises(SearchError, match="kind must be one of"):
        SearchRequest.create("q", kind=["passages"])
    r = SearchRequest.create("q", since=datetime(2025, 1, 1, 15, 30), until=date(2025, 2, 1))
    assert r.since == date(2025, 1, 1) and type(r.since) is date
