import numpy as np
import pytest

from newsletter_archive.search.embeddings import (
    MAX_BATCH,
    EmbeddingError,
    EmbeddingResponseError,
    GeminiEmbedder,
    check_input,
    passage_input,
    query_input,
    summary_input,
    text_key,
    validate_vector,
)
from newsletter_archive.search.text import MAX_EMBED_CHARS, estimate_tokens

from .fakes import FakeSDK
from .test_gemini import make_client


def test_input_formats():
    assert passage_input("T", "H", "Body") == "title: T | text: H\nBody"
    assert passage_input("T", None, "Body") == "title: T | text: Body"
    assert summary_input("T", "Sum") == "title: T | text: Summary\nSum"
    assert query_input("how?") == "task: search result | query: how?"
    assert estimate_tokens("") == 1 and estimate_tokens("abcde") == 2
    assert text_key("a") != text_key("a ")


def test_validate_vector():
    v = validate_vector([3.0, 4.0], 2)
    assert v.dtype == np.float32 and np.allclose(v, [0.6, 0.8])
    for bad, msg in (([1.0], "1 values, expected 2"), ([np.nan, 1], "non-finite"), ([0, 0], "all zeros")):
        with pytest.raises(EmbeddingResponseError, match=msg):
            validate_vector(bad, 2)


def test_batches_each_text_as_its_own_content():
    sdk = FakeSDK()
    client, _, _ = make_client(sdk)
    emb = GeminiEmbedder(client, "gemini-embedding-2", 16, batch_size=2)
    texts = [f"text {i}" for i in range(5)]
    vectors = emb.embed(texts)
    assert [len(c["contents"]) for c in sdk.calls] == [2, 2, 1]
    assert sdk.calls[0]["contents"][1].parts[0].text == "text 1"
    assert (
        sdk.calls[0]["config"].output_dimensionality == 16 and sdk.calls[0]["model"] == "gemini-embedding-2"
    )
    assert len(vectors) == 5 and all(abs(np.linalg.norm(v) - 1) < 1e-5 for v in vectors)
    assert GeminiEmbedder(client, "m", 4, batch_size=500).batch_size == MAX_BATCH
    assert emb.embed([]) == []


def test_unsafe_inputs_are_never_sent():
    sdk = FakeSDK()
    client, _, _ = make_client(sdk)
    with pytest.raises(EmbeddingError, match="not sent"):
        GeminiEmbedder(client, "m", 4).embed(["ok", "x" * (MAX_EMBED_CHARS + 1)])
    with pytest.raises(EmbeddingError, match="empty"):
        GeminiEmbedder(client, "m", 4).embed(["ok", "  "])
    assert sdk.calls == []
    check_input("x" * MAX_EMBED_CHARS)  # exactly at the cap is fine


def test_wrong_count_or_size_fails_the_batch_and_counts_as_a_failure():
    client, _, _ = make_client(FakeSDK(extra_vectors=1))
    with pytest.raises(EmbeddingResponseError, match="asked for 1 embeddings, got 2"):
        GeminiEmbedder(client, "m", 4).embed(["a"])
    assert client.stats.failures == 1
    client, _, _ = make_client(FakeSDK(dim=3), breaker_failures=1)
    with pytest.raises(EmbeddingResponseError, match="3 values, expected 4"):
        GeminiEmbedder(client, "m", 4).embed(["a"])
    assert client.circuit_open
