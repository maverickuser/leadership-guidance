"""Embeddings (gemini-embedding-2, or a local model), plus a content-addressed cache in Postgres.

Verified against the API (PR 3): each text must be its own ``Content`` (a plain list of strings
is embedded as ONE input); at most 100 texts per request; vectors come back L2-normalised;
over-long input is silently truncated and the Developer API can't turn that off, so every input
is capped at ``MAX_EMBED_CHARS`` characters before sending (chunking keeps passages under it).

The model takes no task type: the task goes in the text (``EMBED_INPUT_VERSION``). A local
Qwen3-Embedding model (``local:<name>``) has its own format (``LOCAL_FORMAT``).
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from ..config import is_local_model
from ..errors import ArchiveError
from .gemini import GeminiClient, GeminiError, GeminiResponseError
from .gpu import on_gpu
from .text import MAX_EMBED_CHARS, estimate_tokens
from .versions import EMBED_INPUT_VERSION

log = logging.getLogger("newsletter_archive.search.embeddings")

MAX_BATCH = 100  # API limit per request


class EmbeddingError(GeminiError):
    """An input can't be embedded safely."""


class EmbeddingInputError(EmbeddingError):
    """An input that must not be sent (empty, or too long and would be truncated)."""


class EmbeddingResponseError(GeminiResponseError, EmbeddingError):
    """The API returned embeddings we can't use."""


# --- input formats ------------------------------------------------------------------------------
# Each embedding model family has its own text format, versioned so the post fingerprint changes
# (and posts are re-embedded) only when the format of the model being written changes.

LOCAL_QUERY_INSTRUCTION = "Given a question, find passages from a leadership newsletter that answer it"


def passage_input(title: str, heading: str | None, body: str) -> str:
    """Gemini passage format (EMBED_INPUT_VERSION = 1)."""
    text = f"{heading}\n{body}" if heading else body
    return f"title: {title} | text: {text}"


def summary_input(title: str, summary: str) -> str:
    """Gemini summary format (EMBED_INPUT_VERSION = 1)."""
    return f"title: {title} | text: Summary\n{summary}"


def query_input(query: str) -> str:
    """Gemini query format (EMBED_INPUT_VERSION = 1)."""
    return f"task: search result | query: {query}"


def local_passage_input(title: str, heading: str | None, body: str) -> str:
    """Local Qwen3-Embedding passage: title, heading and text on their own lines, no instruction
    (model card: documents get none). The format the fine-tuned model was trained and measured on."""
    return "\n".join(part for part in (title, heading, body) if part)


def local_summary_input(title: str, summary: str) -> str:
    return f"{title}\n{summary}"


def local_query_input(query: str) -> str:
    """Local Qwen3-Embedding query: the archive instruction it was fine-tuned with."""
    return f"Instruct: {LOCAL_QUERY_INSTRUCTION}\nQuery:{query}"


@dataclass(frozen=True)
class InputFormat:
    """How one model family's inputs are written. ``version`` goes into the post fingerprint."""

    version: int | str
    passage: Callable[[str, str | None, str], str]
    summary: Callable[[str, str], str]
    query: Callable[[str], str]


GEMINI_FORMAT = InputFormat(EMBED_INPUT_VERSION, passage_input, summary_input, query_input)
LOCAL_FORMAT = InputFormat("local-1", local_passage_input, local_summary_input, local_query_input)


def input_format(model: str) -> InputFormat:
    """The input format for an embedding model name (``local:<name>`` or a Gemini model)."""
    return LOCAL_FORMAT if is_local_model(model) else GEMINI_FORMAT


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


# --- local model (openspec use-local-embeddings, lld §4) ---------------------------------------

LOCAL_MAX_TOKENS = 2048  # passages are capped well below this; over it is an error, never cut
WEIGHTS_FILE = "model.safetensors"
# Everything that changes the vectors: the weights, the model config and the tokenizer (it adds
# the end-of-text token that last-token pooling reads).
HASHED_FILES = (WEIGHTS_FILE, "config.json", "tokenizer.json", "tokenizer_config.json")
LOCAL_MODULES = ("torch", "transformers")


class LocalModelError(ArchiveError):
    """The local embedding model can't be used (missing folder, missing packages, other weights)."""


class LocalEmbeddingError(EmbeddingError):
    """The local model failed on one batch (out of memory, busy GPU): that post fails, the run goes on."""


def check_model_folder(folder: Path, name: str) -> None:
    """Raise LocalModelError unless ``folder`` looks like a saved model (config and weights)."""
    if not folder.is_dir():
        raise LocalModelError(
            f"local embedding model {name} not found at {folder}. Set search.local_model_path "
            "in config.yaml or copy the model there."
        )
    missing = [f for f in ("config.json", WEIGHTS_FILE) if not (folder / f).is_file()]
    if missing:
        raise LocalModelError(
            f"{folder} is not a saved model (missing {', '.join(missing)}). Set search.local_model_path "
            "to the folder holding the model."
        )


def weights_sha256(folder: Path) -> str:
    """sha256 over the files that shape the vectors (HASHED_FILES that exist), each with its name,
    read in 1 MB blocks (~3 s for 1.2 GB of weights)."""
    digest = hashlib.sha256()
    for name in HASHED_FILES:
        path = folder / name
        if not path.is_file():
            continue
        digest.update(name.encode() + b"\0")
        with path.open("rb") as f:
            for block in iter(partial(f.read, 1 << 20), b""):
                digest.update(block)
    return digest.hexdigest()


def verify_weights(conn: psycopg.Connection, name: str, sha256: str, record: bool) -> None:
    """Check the weights against the hash first recorded for ``name`` (``public.embedding_models``).
    ``record`` (the indexer, writer role) stores it on first use; a reader (the MCP server) needs
    it to be there already."""
    select = "SELECT weights_sha256 FROM public.embedding_models WHERE name = %s"
    row = conn.execute(select, (name,)).fetchone()
    if row is None and record:
        conn.execute(
            "INSERT INTO public.embedding_models (name, weights_sha256) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (name, sha256),
        )
        row = conn.execute(select, (name,)).fetchone()
    if row is None:
        raise LocalModelError(f"no index has been built with {name} yet; run `archive index` first")
    if row[0] != sha256:
        raise LocalModelError(
            f"the weights in the model folder differ from the ones {name} was indexed with; "
            "give new weights a new name (search.embedding_model: local:<new name>) and run "
            "`archive index --refresh`"
        )


def embed_in_batches(
    texts: Sequence[str],
    encode: Callable[[list[str]], Any],
    count_tokens: Callable[[str], int],
    dim: int,
    batch_size: int,
    max_tokens: int = LOCAL_MAX_TOKENS,
) -> list[np.ndarray]:
    """The model-independent part of local embedding: check every input first (nothing runs if
    one is unsafe), run similar lengths together (less padding), validate each vector and
    return them in input order. ``encode`` maps a batch of texts to one vector per text."""
    for text in texts:
        check_input(text)
        n = count_tokens(text)
        if n > max_tokens:
            raise EmbeddingInputError(
                f"embedding input is {n} tokens, over the local model's {max_tokens}-token cap; "
                "it would be truncated, so it was not embedded"
            )
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    out: list[np.ndarray | None] = [None] * len(texts)
    for start in range(0, len(order), max(1, batch_size)):
        idx = order[start : start + max(1, batch_size)]
        vectors = list(encode([texts[i] for i in idx]))
        if len(vectors) != len(idx):
            raise EmbeddingResponseError(f"asked for {len(idx)} embeddings, got {len(vectors)}")
        for i, v in zip(idx, vectors, strict=True):
            out[i] = validate_vector(v, dim)
    return [v for v in out if v is not None]


class LocalEmbedder:
    """A local Qwen3-Embedding model (``local:<name>``) on Apple MPS, CPU fallback. Loaded
    offline from its folder only: never downloaded, nothing sent anywhere. Last-token pooling
    with left padding, float16 on MPS, as the model was fine-tuned and measured."""

    def __init__(self, model: str, folder: Path, dim: int, batch_size: int = 8) -> None:
        check_model_folder(folder, model)
        self.model, self.dim, self.batch_size, self.folder = model, dim, batch_size, folder
        self._load()

    def _load(self) -> None:  # pragma: no cover - needs torch and the model; checked with -m model
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ModuleNotFoundError as e:
            hint = " (install it with: pip install -e '.[rerank]')" if e.name in LOCAL_MODULES else ""
            raise LocalModelError(f"the local embedding model needs {e.name}{hint}") from None
        self.torch = torch
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        dtype = torch.float16 if self.device == "mps" else torch.float32
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(
                self.folder, padding_side="left", local_files_only=True
            )
            net = AutoModel.from_pretrained(self.folder, dtype=dtype, local_files_only=True)
        except (OSError, ValueError) as e:
            msg = f"could not load the local embedding model from {self.folder}: {e}"
            raise LocalModelError(msg) from None
        self.net = net.to(self.device).eval()
        log.info("local embedding model %s loaded on %s", self.model, self.device)

    def _count_tokens(self, text: str) -> int:  # pragma: no cover - real tokenizer
        return len(self.tokenizer(text, add_special_tokens=True)["input_ids"])

    def _encode(self, batch: list[str]) -> Any:  # pragma: no cover - real model
        torch = self.torch
        enc = self.tokenizer(batch, padding=True, return_tensors="pt").to(self.device)
        with torch.no_grad():
            hidden = self.net(**enc).last_hidden_state[:, -1]
            return hidden.float().cpu().numpy()

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        def encode(batch: list[str]) -> Any:
            try:
                return on_gpu(self._encode, batch)  # never at the same time as the re-ranker
            except RuntimeError as e:  # torch errors (e.g. MPS out of memory) and GpuBusyError
                raise LocalEmbeddingError(f"the local embedding model failed: {e}") from None

        return embed_in_batches(texts, encode, self._count_tokens, self.dim, self.batch_size)

    def weights_sha256(self) -> str:
        return weights_sha256(self.folder)


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
