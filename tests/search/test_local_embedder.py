"""Local embedding model, offline parts (openspec use-local-embeddings, lld §4)."""

from __future__ import annotations

import threading

import numpy as np
import pytest

from newsletter_archive.search import gpu
from newsletter_archive.search.embeddings import (
    EmbeddingInputError,
    EmbeddingResponseError,
    LocalEmbedder,
    LocalModelError,
    check_model_folder,
    embed_in_batches,
    weights_sha256,
)

NAME = "local:test-model"


def model_folder(tmp_path, weights=b"weights"):
    folder = tmp_path / "model"
    folder.mkdir()
    (folder / "config.json").write_text("{}")
    (folder / "model.safetensors").write_bytes(weights)
    return folder


def vec(text, dim=4):
    v = np.zeros(dim, dtype=np.float32)
    v[len(text) % dim] = 3.0
    return v


def encode_by_length(batch):
    return [vec(t) for t in batch]


# spec: search-indexing / Local embedding model (Missing model folder)
def test_missing_folder_names_the_setting(tmp_path):
    with pytest.raises(LocalModelError, match=r"not found at .*search\.local_model_path"):
        check_model_folder(tmp_path / "nope", NAME)


def test_folder_without_weights_is_not_a_model(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    with pytest.raises(LocalModelError, match=r"missing model\.safetensors"):
        check_model_folder(tmp_path, NAME)


def test_weights_hash_covers_weights_config_and_tokenizer(tmp_path):
    folder = model_folder(tmp_path, b"x" * (3 << 20) + b"tail")
    before = weights_sha256(folder)
    assert len(before) == 64 and weights_sha256(folder) == before
    (folder / "model.safetensors").write_bytes(b"x" * (3 << 20) + b"TAIL")
    after_weights = weights_sha256(folder)
    (folder / "tokenizer.json").write_text("{}")
    after_tokenizer = weights_sha256(folder)
    (folder / "config.json").write_text('{"a": 1}')
    assert len({before, after_weights, after_tokenizer, weights_sha256(folder)}) == 4


def test_vectors_come_back_in_input_order_and_normalised():
    texts = ["a much longer text", "b", "mid text"]
    out = embed_in_batches(texts, encode_by_length, len, dim=4, batch_size=2)
    for t, v in zip(texts, out, strict=True):
        assert np.allclose(v, vec(t) / 3.0)


def test_batches_group_similar_lengths():
    seen = []

    def encode(batch):
        seen.append(batch)
        return [vec(t) for t in batch]

    embed_in_batches(["long long text", "a", "bb", "longer still text"], encode, len, dim=4, batch_size=2)
    assert seen == [["a", "bb"], ["long long text", "longer still text"]]


def test_too_long_input_is_refused_before_anything_runs():
    calls = []
    with pytest.raises(EmbeddingInputError, match="over the local model's 5-token cap"):
        embed_in_batches(["ok", "this is far too long"], lambda b: calls.append(b), len, 4, 8, max_tokens=5)
    assert calls == []


def test_empty_input_is_refused():
    with pytest.raises(EmbeddingInputError, match="empty"):
        embed_in_batches(["  "], encode_by_length, len, 4, 8)


# spec: search-indexing / Embedding API use (Bad local vector)
def test_non_finite_vector_is_rejected():
    bad = np.array([np.nan, 1, 1, 1], dtype=np.float32)
    with pytest.raises(EmbeddingResponseError, match="non-finite"):
        embed_in_batches(["x"], lambda b: [bad], len, 4, 8)


def test_wrong_vector_count_is_rejected():
    with pytest.raises(EmbeddingResponseError, match="asked for 2 embeddings, got 1"):
        embed_in_batches(["x", "y"], lambda b: [vec("x")], len, 4, 8)


class FakeLocal(LocalEmbedder):
    """LocalEmbedder with the model swapped for a fake encoder."""

    def _load(self):
        self.active = 0
        self.max_active = 0
        self.lock_held = []

    def _count_tokens(self, text):
        return len(text.split())

    def _encode(self, batch):
        self.lock_held.append(gpu.GPU_LOCK.locked())
        return [vec(t) for t in batch]


def test_local_embedder_checks_the_folder_and_runs_under_the_gpu_lock(tmp_path):
    with pytest.raises(LocalModelError):
        FakeLocal(NAME, tmp_path / "missing", 4)
    emb = FakeLocal(NAME, model_folder(tmp_path), 4, batch_size=1)
    out = emb.embed(["one", "two words"])
    assert len(out) == 2 and emb.lock_held == [True, True]
    assert emb.weights_sha256() == weights_sha256(emb.folder)


class FailingLocal(FakeLocal):
    def _encode(self, batch):
        raise RuntimeError("MPS backend out of memory")


def test_a_model_failure_fails_the_batch_not_the_run(tmp_path):
    """A torch error becomes an EmbeddingError (a GeminiError), which the indexer turns into one failed post."""
    from newsletter_archive.search.embeddings import LocalEmbeddingError
    from newsletter_archive.search.gemini import GeminiError

    emb = FailingLocal(NAME, model_folder(tmp_path), 4)
    with pytest.raises(LocalEmbeddingError, match="out of memory") as e:
        emb.embed(["x"])
    assert isinstance(e.value, GeminiError)


def test_a_busy_gpu_gives_up_after_the_wait():
    with gpu.GPU_LOCK, pytest.raises(gpu.GpuBusyError, match=r"busy for over 0\.05s"):
        gpu.on_gpu(lambda _: None, None, wait=0.05)
    assert gpu.on_gpu(lambda x: x + 1, 1) == 2 and not gpu.GPU_LOCK.locked()


def test_the_lock_is_released_when_the_work_fails():
    with pytest.raises(ValueError):
        gpu.on_gpu(lambda _: (_ for _ in ()).throw(ValueError("boom")), None)
    assert not gpu.GPU_LOCK.locked()


# spec: search-indexing / Local embedding model (Embedder and re-ranker share the GPU)
def test_gpu_lock_serialises_concurrent_work():
    active, peak = [0], [0]
    guard = threading.Lock()

    def work(_):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        threading.Event().wait(0.01)
        with guard:
            active[0] -= 1
        return True

    threads = [threading.Thread(target=gpu.on_gpu, args=(work, None)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1
