"""Re-ranker wrapper: background load, timeout, failures (the real model is checked with -m api)."""

from __future__ import annotations

import threading

import pytest

from newsletter_archive.search.rerank import (
    PREFIX,
    SUFFIX,
    FakeReranker,
    RerankerUnavailableError,
    Reranking,
    format_pair,
    qwen_loader,
)


def loaded(model, timeout=5.0):
    r = Reranking(lambda: model, timeout)
    assert r.wait_loaded(5)
    return r


def test_prompt_matches_the_model_card():
    assert PREFIX.startswith("<|im_start|>system\nJudge whether the Document meets the requirements")
    assert PREFIX.endswith('can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
    assert SUFFIX == "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    assert format_pair("q", "d", "i") == "<Instruct>: i\n<Query>: q\n<Document>: d"


def test_fake_reranker_prefers_documents_with_query_words():
    scores = FakeReranker().score("delegate work", ["we delegate work here", "coffee machine"])
    assert scores[0] > scores[1] == 0


def test_scores_once_loaded():
    r = loaded(FakeReranker())
    assert r.status == "ready" and len(r.score("q", ["a", "b"])) == 2
    r.close()


def test_unavailable_while_loading_and_after_a_failed_load():
    gate = threading.Event()

    def slow():
        gate.wait(5)
        return FakeReranker()

    r = Reranking(slow, 5)
    assert r.status == "loading"
    with pytest.raises(RerankerUnavailableError, match="loading"):
        r.score("q", ["a"])
    gate.set()
    assert r.wait_loaded(5) and r.status == "ready"

    def broken():
        raise ImportError("No module named 'torch'")

    bad = Reranking(broken, 5)
    assert bad.wait_loaded(5) and "No module named 'torch'" in bad.status
    with pytest.raises(RerankerUnavailableError, match="unavailable"):
        bad.score("q", ["a"])


def test_failure_timeout_busy_and_wrong_count():
    with pytest.raises(RerankerUnavailableError, match="failed: RuntimeError: boom"):
        loaded(FakeReranker(fail=RuntimeError("boom"))).score("q", ["a"])
    slow = loaded(FakeReranker(delay=0.5), timeout=0.05)
    with pytest.raises(RerankerUnavailableError, match=r"longer than 0\.05s"):
        slow.score("q", ["a"])
    with pytest.raises(RerankerUnavailableError, match="busy"):
        slow.score("q", ["a"])

    class Short:
        def score(self, query, documents):
            return [1.0]

    with pytest.raises(RerankerUnavailableError, match="wrong number"):
        loaded(Short()).score("q", ["a", "b"])


def test_qwen_loader_is_lazy(tmp_path):
    assert callable(qwen_loader("some/model", tmp_path))  # nothing imported or downloaded yet


def test_hub_goes_offline_once_the_model_is_cached(tmp_path, monkeypatch):
    from newsletter_archive.search.rerank import configure_hub, is_cached

    for var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
        monkeypatch.delenv(var, raising=False)
    assert not is_cached("Org/Model", tmp_path)
    configure_hub("Org/Model", tmp_path)
    import os

    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1" and "HF_HUB_OFFLINE" not in os.environ
    (tmp_path / "models--Org--Model" / "snapshots" / "abc123").mkdir(parents=True)
    configure_hub("Org/Model", tmp_path)
    assert is_cached("Org/Model", tmp_path) and os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_concurrent_calls_never_queue_behind_each_other():
    r = loaded(FakeReranker(delay=0.3))
    errors, started = [], threading.Event()

    def first():
        started.set()
        r.score("q", ["a"])

    t = threading.Thread(target=first)
    t.start()
    started.wait(2)
    for _ in range(100):  # until the first call has been submitted
        if r._busy is not None:
            break
        threading.Event().wait(0.005)
    try:
        r.score("q", ["b"])
    except RerankerUnavailableError as e:
        errors.append(str(e))
    t.join()
    assert errors == ["re-ranker still busy with an earlier search"]


def test_install_hint_only_for_the_missing_extra():
    def missing(name):
        def load():
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)

        return load

    r = Reranking(missing("torch"), 5)
    assert r.wait_loaded(5) and "pip install -e '.[rerank]'" in r.status
    r = Reranking(missing("some_native_lib"), 5)
    assert r.wait_loaded(5) and "pip install" not in r.status and "some_native_lib" in r.status
