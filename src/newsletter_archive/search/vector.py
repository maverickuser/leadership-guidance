"""Semantic search: exact cosine distance over passage and summary vectors (no approximate
index: at a few thousand rows an exact scan takes milliseconds and never drops filtered rows)."""

from __future__ import annotations

import weakref

import numpy as np
import psycopg
from pgvector import HalfVector
from pgvector.psycopg import register_vector
from psycopg import sql

from .filters import Filters, filter_sql

_registered: weakref.WeakSet[psycopg.Connection] = weakref.WeakSet()


def ensure_vector_types(conn: psycopg.Connection) -> None:
    """Register the pgvector types on ``conn`` (once per connection)."""
    if conn not in _registered:
        register_vector(conn)
        _registered.add(conn)


def vector_search(
    conn: psycopg.Connection, schema: str, query_vector: np.ndarray, filters: Filters, limit: int
) -> list[str]:
    """Passage ids, nearest first (ties: passage id). Works on any connection: the pgvector
    types are registered here (once per connection)."""
    ensure_vector_types(conn)
    where, params = filter_sql(filters)
    statement = sql.SQL(
        "SELECT passage_id FROM {schema}.passages WHERE TRUE{where} "
        "ORDER BY embedding <=> %s, passage_id LIMIT %s"
    ).format(schema=sql.Identifier(schema), where=where)
    rows = conn.execute(statement, [*params, HalfVector(query_vector), limit]).fetchall()
    return [r[0] for r in rows]
