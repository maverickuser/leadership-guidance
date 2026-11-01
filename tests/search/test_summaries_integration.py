import pytest

from newsletter_archive.search import db
from newsletter_archive.search.models import Section
from newsletter_archive.search.summaries import CachedSummarizer, Summary, SummaryCache, summary_key

pytestmark = pytest.mark.db

SECTIONS = [Section(None, "Intro text."), Section("Why", "Body text.")]


class FakeSummarizer:
    model = "fake-model"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def summarize(self, title, sections):
        self.calls.append(title)
        return Summary(f"Summary of {title}.", 100, 20)


@pytest.fixture
def conn(db_config):
    with db.connect(db_config, "writer") as c:
        db.apply_migrations(c)
        yield c


def test_cache_round_trip(conn):
    cache = SummaryCache(conn)
    assert cache.get("k") is None and not cache.has("k")
    cache.put("k", "m", Summary("first", 1, 2))
    cache.put("k", "m", Summary("second", 1, 2))
    assert cache.get("k") == "second" and cache.has("k")


# spec: search-indexing / Post summaries / Unchanged post
def test_unchanged_post_is_never_summarised_twice(conn):
    fake = FakeSummarizer()
    s = CachedSummarizer(fake, SummaryCache(conn))
    assert s.missing_tokens([("A", SECTIONS), ("B", SECTIONS)]) > 0
    assert s.summarize("A", SECTIONS) == "Summary of A."
    assert s.summarize("A", SECTIONS) == "Summary of A."
    assert s.summarize("A", [Section(None, "Edited.")]) == "Summary of A."
    assert fake.calls == ["A", "A"]  # the edit regenerated it; the repeat didn't
    assert s.missing_tokens([("A", SECTIONS)]) == 0
    st = s.stats
    assert (st.requested, st.cache_hits, st.generated, st.input_tokens, st.output_tokens) == (
        3,
        1,
        2,
        200,
        40,
    )
    row = conn.execute(
        "SELECT model, prompt_version, input_tokens FROM summary_cache WHERE summary_sha256 = %s",
        (summary_key("fake-model", "A", SECTIONS),),
    ).fetchone()
    assert row == ("fake-model", 1, 100)
