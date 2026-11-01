"""Semantic search: exact cosine distance over passage and summary vectors (no approximate
index: at a few thousand rows an exact scan takes milliseconds and never drops filtered rows)."""

from __future__ import annotations

import numpy as np
import psycopg
from pgvector import HalfVector
from psycopg import sql

from .keyword import Filters, filter_sql


def vector_search(
    conn: psycopg.Connection, schema: str, query_vector: np.ndarray, filters: Filters, limit: int
) -> list[str]:
    """Passage ids, nearest first (ties: passage id)."""
    where, params = filter_sql(filters)
    statement = sql.SQL(
        "SELECT passage_id FROM {schema}.passages WHERE TRUE{where} "
        "ORDER BY embedding <=> %s, passage_id LIMIT %s"
    ).format(schema=sql.Identifier(schema), where=where)
    rows = conn.execute(statement, [*params, HalfVector(query_vector), limit]).fetchall()
    return [r[0] for r in rows]
