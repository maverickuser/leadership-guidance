import numpy as np
import pytest

from newsletter_archive.search import db
from newsletter_archive.search.embeddings import CachedEmbedder, EmbeddingCache, text_key

from .fakes import FakeEmbedder

pytestmark = pytest.mark.db


@pytest.fixture
def conn(db_config):
    with db.connect(db_config, "writer") as c:
        db.apply_migrations(c)
        yield c


def test_cache_round_trip_and_isolation(conn):
    cache = EmbeddingCache(conn, "m", 4)
    v = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32)
    cache.put_many({"k1": v})
    cache.put_many({"k1": v})  # re-put only touches it
    cache.put_many({})
    got = cache.get_many(["k1", "k2"])
    assert list(got) == ["k1"] and np.allclose(got["k1"], v) and got["k1"].dtype == np.float32
    assert EmbeddingCache(conn, "m", 8).get_many(["k1"]) == {}
    assert EmbeddingCache(conn, "other", 4).get_many(["k1"]) == {}
    assert cache.get_many([]) == {}


# spec: search-indexing / Change detection / Nothing changed (embedding side)
def test_identical_text_is_never_embedded_twice(conn):
    fake = FakeEmbedder(dim=4)
    embedder = CachedEmbedder(fake, EmbeddingCache(conn, fake.model, fake.dim))
    first = embedder.embed(["a", "b", "a"])
    assert fake.batches == [["a", "b"]] and np.allclose(first[0], first[2])
    assert embedder.missing_tokens(["a", "b", "c", "c"]) == 1
    second = embedder.embed(["b", "a", "c"])
    assert fake.batches == [["a", "b"], ["c"]]
    assert np.allclose(second[1], first[0])
    s = embedder.stats
    assert (s.requested, s.cache_hits, s.embedded, s.tokens) == (6, 3, 3, 3)
    row = conn.execute(
        "SELECT count(*) FROM embedding_cache WHERE embed_sha256 = %s", (text_key("a"),)
    ).fetchone()
    assert row == (1,)
