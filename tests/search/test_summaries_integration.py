import pytest

from newsletter_archive.search import db
from newsletter_archive.search.models import Section
from newsletter_archive.search.summaries import CachedSummarizer, Summary, SummaryCache

pytestmark = pytest.mark.db

SECTIONS = [Section(None, "Intro text."), Section("Why", "Body text.")]


class FakeSummarizer:
    model = "fake-model"
    thinking_level = "low"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def summarize(self, title, sections):
        self.calls.append(title)
        return Summary(f"Summary of {title}.", 100, 20 * len(self.calls))


@pytest.fixture
def conn(db_config):
    with db.connect(db_config, "writer") as c:
        db.apply_migrations(c)
        yield c


def test_cache_round_trip(conn):
    cache = SummaryCache(conn)
    assert cache.get("k") is None and cache.has_many(["k"]) == set() and cache.has_many([]) == set()
    cache.put("k", "m", Summary("first", 1, 2))
    cache.put("k", "m2", Summary("second", 3, 4))
    assert cache.get("k") == "second" and cache.has_many(["k", "x"]) == {"k"}
    row = conn.execute("SELECT model, input_tokens, output_tokens FROM summary_cache").fetchone()
    assert row == ("m2", 3, 4)


def test_reads_dont_write_and_flush_touches_once(conn):
    cache = SummaryCache(conn)
    cache.put("k", "m", Summary("text", 1, 2))
    conn.execute("UPDATE summary_cache SET last_used_at = now() - interval '10 days'")
    s = CachedSummarizer(FakeSummarizer(), cache)
    s._hits.append("k")
    assert cache.get("k") == "text"
    old = conn.execute("SELECT last_used_at < now() - interval '9 days' FROM summary_cache").fetchone()
    assert old == (True,)
    s.flush()
    fresh = conn.execute("SELECT last_used_at > now() - interval '1 minute' FROM summary_cache").fetchone()
    assert fresh == (True,) and s._hits == []
    cache.touch([])


# spec: search-indexing / Post summaries / Unchanged post
def test_unchanged_post_is_never_summarised_twice(conn):
    fake = FakeSummarizer()
    s = CachedSummarizer(fake, SummaryCache(conn))
    assert s.missing_tokens([("A", SECTIONS), ("A", SECTIONS), ("B", SECTIONS)]) == 2 * s.missing_tokens(
        [("A", SECTIONS)]
    )
    text, key = s.summarize("A", SECTIONS)
    assert text == "Summary of A." and key == s.key("A", SECTIONS)
    assert s.summarize("A", SECTIONS)[0] == "Summary of A."
    s.summarize("A", [Section(None, "Edited.")])
    assert fake.calls == ["A", "A"]  # the edit regenerated it; the repeat didn't
    assert s.missing_tokens([("A", SECTIONS)]) == 0
    st = s.stats
    assert (st.requested, st.cache_hits, st.generated, st.input_tokens, st.output_tokens) == (
        3,
        1,
        2,
        200,
        60,
    )
