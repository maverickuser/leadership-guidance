import numpy as np
import pytest

from newsletter_archive.search.embeddings import (
    MAX_BATCH,
    EmbeddingError,
    GeminiEmbedder,
    estimate_tokens,
    passage_input,
    query_input,
    summary_input,
    text_key,
    validate_vector,
)

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
        with pytest.raises(EmbeddingError, match=msg):
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


def test_too_long_input_is_never_sent():
    sdk = FakeSDK()
    client, _, _ = make_client(sdk)
    with pytest.raises(EmbeddingError, match="not sent"):
        GeminiEmbedder(client, "m", 4).embed(["ok", "x" * 40_000])
    assert sdk.calls == []


def test_wrong_count_or_size_fails_the_batch():
    client, _, _ = make_client(FakeSDK(extra_vectors=1))
    with pytest.raises(EmbeddingError, match="asked for 1 embeddings, got 2"):
        GeminiEmbedder(client, "m", 4).embed(["a"])
    client, _, _ = make_client(FakeSDK(dim=3))
    with pytest.raises(EmbeddingError, match="3 values, expected 4"):
        GeminiEmbedder(client, "m", 4).embed(["a"])
