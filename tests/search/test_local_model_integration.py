"""Weights recorded per local model name (migration 003; openspec use-local-embeddings, lld §4)."""

from __future__ import annotations

import pytest

from newsletter_archive.search import db
from newsletter_archive.search.embeddings import LocalModelError, verify_weights

pytestmark = pytest.mark.db

A, B = "a" * 64, "b" * 64


def test_first_index_records_the_weights_and_later_runs_must_match(db_config):
    with db.connect(db_config, "writer") as conn:
        db.apply_migrations(conn)
        conn.execute("DELETE FROM public.embedding_models")
        verify_weights(conn, "local:m", A, record=True)
        verify_weights(conn, "local:m", A, record=True)  # same weights: fine
        with pytest.raises(LocalModelError, match="give new weights a new name"):
            verify_weights(conn, "local:m", B, record=True)
        conn.commit()


def test_reader_needs_the_record_and_can_read_it(db_config):
    with db.connect(db_config, "writer") as conn:
        db.apply_migrations(conn)
        conn.execute("DELETE FROM public.embedding_models")
        conn.execute(
            "INSERT INTO public.embedding_models (name, weights_sha256) VALUES ('local:m', %s)", (A,)
        )
        conn.commit()
    with db.connect(db_config, "reader") as conn:
        verify_weights(conn, "local:m", A, record=False)
        with pytest.raises(LocalModelError, match=r"run `archive index` first"):
            verify_weights(conn, "local:other", A, record=False)
        with pytest.raises(LocalModelError, match="differ"):
            verify_weights(conn, "local:m", B, record=False)


def test_hash_must_be_hex(db_config):
    import psycopg

    with db.connect(db_config, "writer") as conn:
        db.apply_migrations(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO public.embedding_models (name, weights_sha256) VALUES ('x', 'nope')")
        conn.rollback()
