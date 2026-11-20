"""Which embedder to use for an index version's model (openspec use-local-embeddings, lld §5).

Every caller (the indexer, the MCP server, the evaluation and the quality gate) asks
``embedder_for(config, ...)`` for a factory ``(model, dim) -> Embedder``. A ``local:<name>`` model
gets the local embedder, loaded once per process and shared; a ``gemini-…`` model gets the Gemini
embedder. The model comes from the index version, not the config, so after a rollback to a
Gemini version its queries are still embedded with Gemini.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from ..config import Config, is_local_model
from .embeddings import Embedder, LocalEmbedder, LocalModelError, verify_weights

log = logging.getLogger("newsletter_archive.search.embedders")

EmbedderFactory = Callable[[str, int], Embedder]
LocalLoader = Callable[[str, Any, int, int], Embedder]


def _load_local(model: str, folder: Any, dim: int, batch_size: int) -> Embedder:  # pragma: no cover
    return LocalEmbedder(model, folder, dim, batch_size)


class LocalModelLoading(LocalModelError):
    """A query arrived while the model is still loading (the search runs on keywords meanwhile)."""


class LocalModels:
    """Local embedders shared by every caller in this process, loaded on first use. Loading
    takes seconds, so ``preload`` starts it in the background (the MCP server at start-up)."""

    def __init__(self, config: Config, loader: LocalLoader | None = None, check: str | None = None) -> None:
        """``check``: None, "record" (the indexer stores the weights hash on first use) or
        "verify" (a reader needs the recorded hash to match)."""
        self.config = config
        self.loader = loader
        self.check = check
        self._lock = threading.Lock()
        self._loaded: dict[tuple[str, int], Embedder] = {}
        self._refused: dict[tuple[str, int], LocalModelError] = {}

    def get(self, model: str, dim: int, batch_size: int, wait: bool = True) -> Embedder:
        """The shared embedder, loading it on first use. ``wait=False`` (queries) raises
        LocalModelLoading instead of waiting while another thread loads. Weights that fail the
        hash check are refused for the life of the process, without reloading them each time."""
        key = (model, dim)
        if key in self._refused:
            raise self._refused[key]
        if key in self._loaded:
            return self._loaded[key]
        if not self._lock.acquire(blocking=wait):
            raise LocalModelLoading(f"the local embedding model {model} is still loading")
        try:
            if key in self._refused:
                raise self._refused[key]
            if key not in self._loaded:
                folder = self.config.local_model_dir(model)
                embedder = (self.loader or _load_local)(model, folder, dim, batch_size)
                if self.check == "verify":
                    try:
                        self._verify(embedder)
                    except LocalModelError as e:
                        self._refused[key] = e
                        raise
                self._loaded[key] = embedder
            return self._loaded[key]
        finally:
            self._lock.release()

    def _verify(self, embedder: Embedder) -> None:
        from . import db

        sha = getattr(embedder, "weights_sha256", None)
        if sha is None:
            return
        with db.connect(self.config, "reader") as conn:
            verify_weights(conn, embedder.model, sha(), record=False)

    def preload(self, model: str, dim: int) -> threading.Thread:
        def run() -> None:
            try:
                self.get(model, dim, 1)
                log.info("local embedding model %s ready", model)
            except Exception as e:  # the search reports it on the first query
                log.warning("local embedding model %s unavailable: %s", model, e)

        thread = threading.Thread(target=run, name="local-embedder-load", daemon=True)
        thread.start()
        return thread

    def preload_live(self) -> threading.Thread | None:
        """Preload the live version's model when it is local (the MCP server at start-up), else
        the configured one when that is local. Never blocks start-up."""
        from . import db

        s = self.config.search
        model, dim = s.embedding_model, s.embedding_dimension
        try:
            with db.connect(self.config, "reader") as conn:
                live = db.active_generation(conn)
            if live is not None:
                model, dim = live.embedding_model, live.embedding_dim
        except Exception as e:  # database down or empty: fall back to the config
            log.info("live index version unknown at start-up (%s); preloading the configured model", e)
        return self.preload(model, dim) if is_local_model(model) else None


def embedder_for(
    config: Config,
    *,
    for_queries: bool,
    local: LocalModels | None = None,
    gemini: EmbedderFactory | None = None,
) -> EmbedderFactory:
    """A factory ``(model, dim) -> Embedder`` that picks the local or the Gemini embedder by the
    model name. Queries embed one text at a time; indexing uses ``search.local_batch_size``.
    The Gemini side is built only when a Gemini model is asked for, so a local-only setup never
    needs ``GEMINI_API_KEY``."""
    local = local or LocalModels(config, check="verify" if for_queries else None)
    batch = 1 if for_queries else config.search.local_batch_size
    gemini_made: list[EmbedderFactory] = []

    def make(model: str, dim: int) -> Embedder:
        if is_local_model(model):
            return local.get(model, dim, batch, wait=not for_queries)
        if not gemini_made:
            gemini_made.append(gemini or _gemini_factory(config, for_queries))
        return gemini_made[0](model, dim)

    return make


def _gemini_factory(config: Config, for_queries: bool) -> EmbedderFactory:  # pragma: no cover - real API
    if for_queries:
        from .service import gemini_query_embedder

        return gemini_query_embedder(config)  # type: ignore[return-value]
    from .embeddings import GeminiEmbedder
    from .gemini import GeminiClient

    s = config.search

    def make(model: str, dim: int) -> Embedder:
        client = GeminiClient(
            config.gemini_api_key,
            rpm=s.embed_rpm,
            tpm=s.embed_tpm,
            breaker_failures=s.circuit_breaker_failures,
            timeout_seconds=s.gemini_timeout_seconds,
        )
        return GeminiEmbedder(client, model, dim, s.embed_batch_size)

    return make
