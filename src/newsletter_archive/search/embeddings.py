"""Embeddings with gemini-embedding-2, plus a content-addressed cache in Postgres.

Verified against the API (PR 3): each text must be its own ``Content`` (a plain list of strings
is embedded as ONE input); at most 100 texts per request; vectors come back L2-normalised;
over-long input is silently truncated and the Developer API can't turn that off, so every input
is capped at ``MAX_EMBED_CHARS`` characters before sending (chunking keeps passages under it).

The model takes no task type: the task goes in the text (``EMBED_INPUT_VERSION``).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from .gemini import GeminiClient, GeminiError, GeminiResponseError
from .text import MAX_EMBED_CHARS, estimate_tokens

log = logging.getLogger("newsletter_archive.search.embeddings")

MAX_BATCH = 100  # API limit per request


class EmbeddingError(GeminiError):
    """An input can't be embedded safely."""


class EmbeddingInputError(EmbeddingError):
    """An input that must not be sent (empty, or too long and would be truncated)."""


class EmbeddingResponseError(GeminiResponseError, EmbeddingError):
    """The API returned embeddings we can't use."""


# --- input formats (EMBED_INPUT_VERSION = 1) ---------------------------------------------------


def passage_input(title: str, heading: str | None, body: str) -> str:
    text = f"{heading}\n{body}" if heading else body
    return f"title: {title} | text: {text}"


def summary_input(title: str, summary: str) -> str:
    return f"title: {title} | text: Summary\n{summary}"


def query_input(query: str) -> str:
    return f"task: search result | query: {query}"


def check_input(text: str) -> None:
    """Raise EmbeddingError for an input the API would reject or silently truncate. The indexer
    calls this per post first, so one bad input fails only its post."""
    if not text.strip():
        raise EmbeddingInputError("empty embedding input")
    if len(text) > MAX_EMBED_CHARS:
        raise EmbeddingInputError(
            f"embedding input is {len(text)} characters, over the {MAX_EMBED_CHARS}-character cap; "
            "it would be truncated, so it was not sent"
        )


def text_key(text: str) -> str:
    """Cache key of an exact embedding input."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_vector(values: Sequence[float], dim: int) -> np.ndarray:
    """Exactly ``dim`` finite values with a non-zero length; returned L2-normalised float32."""
    vec = np.asarray(values, dtype=np.float32)
    if vec.shape != (dim,):
        raise EmbeddingResponseError(f"embedding has {vec.size} values, expected {dim}")
    if not np.all(np.isfinite(vec)):
        raise EmbeddingResponseError("embedding contains non-finite values")
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        raise EmbeddingResponseError("embedding is all zeros")
    normalised: np.ndarray = (vec / norm).astype(np.float32)
    return normalised


class Embedder(Protocol):
    model: str
    dim: int
    batch_size: int

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]: ...


@dataclass
class GeminiEmbedder:
    client: GeminiClient
    model: str
    dim: int
    batch_size: int = 50

    def __post_init__(self) -> None:
        self.batch_size = max(1, min(self.batch_size, MAX_BATCH))

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        """Embed texts in order, in batches. Raises EmbeddingError for an unsafe input (nothing
        is sent) or an unusable response (counted by the circuit breaker); Gemini errors
        propagate (see GeminiClient)."""
        for text in texts:
            check_input(text)
        out: list[np.ndarray] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            tokens = sum(estimate_tokens(t) for t in batch)
            vectors: list[np.ndarray] = []

            def check(response: Any, batch: list[str] = batch, vectors: list[np.ndarray] = vectors) -> None:
                embeddings = getattr(response, "embeddings", None) or []
                if len(embeddings) != len(batch):
                    raise EmbeddingResponseError(f"asked for {len(batch)} embeddings, got {len(embeddings)}")
                vectors.extend(validate_vector(e.values, self.dim) for e in embeddings)

            self.client.call(partial(self._request, batch=batch), tokens, "embedding", check)
            out.extend(vectors)
        return out

    def _request(self, sdk: Any, batch: list[str]) -> Any:
        from google.genai import types

        return sdk.models.embed_content(
            model=self.model,
            contents=[types.Content(parts=[types.Part(text=t)]) for t in batch],
            config=types.EmbedContentConfig(output_dimensionality=self.dim),
        )


def _to_array(value: Any) -> np.ndarray:
    """pgvector returns its own Vector type (``to_numpy``) or a numpy array, by column type."""
    raw = value.to_numpy() if hasattr(value, "to_numpy") else value
    array: np.ndarray = np.asarray(raw, dtype=np.float32)
    return array


class EmbeddingCache:
    """``public.embedding_cache``: vectors by (sha256 of exact input, model, dim), full precision."""

    def __init__(self, conn: psycopg.Connection, model: str, dim: int) -> None:
        register_vector(conn)
        self.conn = conn
        self.model = model
        self.dim = dim

    def get_many(self, keys: Sequence[str]) -> dict[str, np.ndarray]:
        if not keys:
            return {}
        rows = self.conn.execute(
            "UPDATE public.embedding_cache SET last_used_at = now() "
            "WHERE model = %s AND dim = %s AND embed_sha256 = ANY(%s) RETURNING embed_sha256, embedding",
            (self.model, self.dim, list(keys)),
        ).fetchall()
        return {k: _to_array(v) for k, v in rows}

    def has_many(self, keys: Sequence[str]) -> set[str]:
        """Which keys are cached. Read-only (doesn't touch ``last_used_at``)."""
        if not keys:
            return set()
        rows = self.conn.execute(
            "SELECT embed_sha256 FROM public.embedding_cache "
            "WHERE model = %s AND dim = %s AND embed_sha256 = ANY(%s)",
            (self.model, self.dim, list(keys)),
        ).fetchall()
        return {r[0] for r in rows}

    def missing(self, texts: Sequence[str]) -> tuple[int, int]:
        """(estimated tokens to embed the texts not cached, how many of ``texts`` are cached).
        Read-only, so a dry run changes nothing."""
        unique = {text_key(t): t for t in texts}
        cached = self.has_many(sorted(unique))
        tokens = sum(estimate_tokens(t) for k, t in unique.items() if k not in cached)
        return tokens, sum(1 for t in texts if text_key(t) in cached)

    def put_many(self, vectors: dict[str, np.ndarray]) -> None:
        if not vectors:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO public.embedding_cache (embed_sha256, model, dim, embedding) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (embed_sha256, model, dim) "
                "DO UPDATE SET last_used_at = now()",
                [(k, self.model, self.dim, v) for k, v in vectors.items()],
            )


@dataclass
class EmbedStats:
    requested: int = 0
    cache_hits: int = 0
    embedded: int = 0
    tokens: int = 0


@dataclass
class CachedEmbedder:
    """Embeds through the cache: identical input text is never sent twice for a model and dim."""

    embedder: Embedder
    cache: EmbeddingCache
    stats: EmbedStats = field(default_factory=EmbedStats)

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        keys = [text_key(t) for t in texts]
        self.stats.requested += len(texts)
        found = self.cache.get_many(sorted(set(keys)))
        missing: dict[str, str] = {}
        for key, text in zip(keys, texts, strict=True):
            if key not in found and key not in missing:
                missing[key] = text
        self.stats.cache_hits += len(texts) - len(missing)  # inputs served without an API call
        todo = list(missing.items())
        step = max(1, getattr(self.embedder, "batch_size", MAX_BATCH))
        for start in range(0, len(todo), step):
            # Cache each batch as soon as it's back: a later failure never wastes paid work.
            chunk = todo[start : start + step]
            vectors = self.embedder.embed([t for _, t in chunk])
            new = {k: v for (k, _), v in zip(chunk, vectors, strict=True)}
            self.cache.put_many(new)
            found.update(new)
            self.stats.embedded += len(chunk)
            self.stats.tokens += sum(estimate_tokens(t) for _, t in chunk)
        return [found[k] for k in keys]

    def missing_tokens(self, texts: Sequence[str]) -> int:
        """Estimated tokens to embed the texts not yet cached (for the spending ceiling).
        Read-only, so a dry run changes nothing."""
        return self.cache.missing(texts)[0]
