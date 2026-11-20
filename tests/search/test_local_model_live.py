"""Opt-in check of the real local embedding model (needs the rerank extra and the model folder).

Run: ARCHIVE_MODEL_TESTS=1 pytest -m model tests/search/test_local_model_live.py
Skipped otherwise, and never in CI. Nothing is downloaded: the model loads from its folder only.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from newsletter_archive.config import is_local_model, load_config

pytestmark = pytest.mark.model
REPO = Path(__file__).resolve().parents[2]


def test_real_local_model_ranks_a_matching_passage_first():
    if os.environ.get("ARCHIVE_MODEL_TESTS") != "1":
        pytest.skip("set ARCHIVE_MODEL_TESTS=1 to load the real local model")
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    from newsletter_archive.search.embeddings import LOCAL_FORMAT, LocalEmbedder

    cfg = load_config(REPO / "config.yaml", REPO / ".env")
    name = (
        cfg.search.embedding_model
        if is_local_model(cfg.search.embedding_model)
        else "local:qwen3-embedding-0.6b-archive-v5"
    )
    emb = LocalEmbedder(name, cfg.local_model_dir(name), 1024)
    q = emb.embed([LOCAL_FORMAT.query("how do I hand off work to my team?")])[0]
    docs = emb.embed(
        [
            LOCAL_FORMAT.passage(
                "Delegation", None, "Hand over the outcome, not the task, and let them own it."
            ),
            LOCAL_FORMAT.passage("Weather", None, "Rain is likely tomorrow afternoon with light winds."),
        ]
    )
    assert q.shape == (1024,) and abs(float(np.linalg.norm(q)) - 1) < 1e-3
    assert float(q @ docs[0]) > float(q @ docs[1])
