"""Opt-in checks against the real Gemini API (costs a fraction of a cent).

Run: ARCHIVE_API_TESTS=1 pytest -m api tests/search/test_api_smoke.py
Skipped otherwise, and never in CI. Uses GEMINI_API_KEY from the environment or .env.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import numpy as np
import pytest

from newsletter_archive.config import load_config
from newsletter_archive.search.embeddings import GeminiEmbedder, passage_input, query_input
from newsletter_archive.search.gemini import GeminiClient

from .. import conftest

pytestmark = pytest.mark.api
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def live_client(monkeypatch):
    if os.environ.get("ARCHIVE_API_TESTS") != "1":
        pytest.skip("set ARCHIVE_API_TESTS=1 to call the real Gemini API")
    key = load_config(REPO / "config.yaml", REPO / ".env").gemini_api_key
    if not key:
        pytest.skip("GEMINI_API_KEY is not set")
    # these tests are allowed to reach the internet
    monkeypatch.setattr(socket.socket, "connect", conftest._real_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", conftest._real_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", conftest._real_getaddrinfo)
    return GeminiClient(key, rpm=30, tpm=100_000)


def test_real_embeddings_are_separate_normalised_and_meaningful(live_client):
    emb = GeminiEmbedder(live_client, "gemini-embedding-2", 3072, batch_size=2)
    vectors = emb.embed(
        [
            passage_input("Delegation", "Why it matters", "Delegating work builds trust in your team."),
            passage_input("Hiring", None, "How to interview a first-time manager candidate."),
            query_input("how should I delegate work to my team"),
        ]
    )
    assert len(vectors) == 3 and all(v.shape == (3072,) for v in vectors)
    assert all(abs(float(np.linalg.norm(v)) - 1) < 1e-3 for v in vectors)
    assert float(vectors[2] @ vectors[0]) > float(vectors[2] @ vectors[1])
    assert live_client.stats.calls == 2
