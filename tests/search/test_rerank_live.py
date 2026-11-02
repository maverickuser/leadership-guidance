"""Opt-in check of the real Qwen3 re-ranker (needs the rerank extra; first run downloads ~1.2 GB).

Run: ARCHIVE_API_TESTS=1 pytest -m api tests/search/test_rerank_live.py
Skipped otherwise, and never in CI.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest

from newsletter_archive.config import load_config

from .. import conftest

pytestmark = pytest.mark.api
REPO = Path(__file__).resolve().parents[2]


def test_real_reranker_scores_relevance(monkeypatch):
    if os.environ.get("ARCHIVE_API_TESTS") != "1":
        pytest.skip("set ARCHIVE_API_TESTS=1 to run the real re-ranker")
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    from newsletter_archive.search.rerank import Qwen3Reranker

    monkeypatch.setattr(socket.socket, "connect", conftest._real_connect)  # first run downloads
    monkeypatch.setattr(socket.socket, "connect_ex", conftest._real_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", conftest._real_getaddrinfo)
    cfg = load_config(REPO / "config.yaml", REPO / ".env")
    model = Qwen3Reranker(cfg.search.reranker_model, cfg.output_dir / "models")
    docs = [
        "The weather report says rain is likely tomorrow afternoon.",
        "Delegation means handing over ownership of an outcome and trusting the person to deliver it.",
    ] * 20
    model.score("warm up", docs[:2])
    started = time.monotonic()
    scores = model.score("how do I delegate work to my team", docs)
    elapsed = time.monotonic() - started
    assert scores[1] > 0.5 > scores[0] and all(0 <= s <= 1 for s in scores)
    print(f"re-ranked 40 pairs on {model.device} in {elapsed:.2f}s")
    assert elapsed < 10  # rerank_timeout_seconds
