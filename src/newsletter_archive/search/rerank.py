"""Re-ranking: a local model reads the query with each candidate and scores how well it answers.

``Qwen3Reranker`` runs ``Qwen/Qwen3-Reranker-0.6B`` with ``transformers`` on Apple MPS (CPU
fallback). The prompt and scoring follow the model card exactly: a yes/no judgement, score =
probability of "yes". The model is downloaded once into ``{output_dir}/models/`` and loaded
offline after that. torch and transformers are an optional install (``pip install -e
'.[rerank]'``); without them search still works, just not re-ranked.

``Reranking`` loads the model in the background and enforces the timeout, so a slow or missing
re-ranker never blocks a search (the service returns the combined order instead).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("newsletter_archive.search.rerank")

INSTRUCTION = "Given a question, find passages from a leadership newsletter that answer it"
MAX_TOKENS = 512  # per query + document pair, prompt included
BATCH_SIZE = 8

# From the model card (Qwen/Qwen3-Reranker-0.6B, "Using Transformers"), verbatim.
PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the "
    'Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def format_pair(query: str, document: str, instruction: str = INSTRUCTION) -> str:
    return f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}"


class RerankerUnavailableError(Exception):
    """The re-ranker can't score right now (not installed, still loading, failed, timed out)."""


class Reranker(Protocol):
    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        """One relevance score per document, higher is better."""
        ...


class Qwen3Reranker:  # pragma: no cover - needs torch and the model; checked live (-m api)
    def __init__(self, model_name: str, cache_dir: Path, batch_size: int = BATCH_SIZE) -> None:
        configure_hub(model_name, cache_dir)  # before transformers reads its settings
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch = torch
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        dtype = torch.float16 if self.device == "mps" else torch.float32
        kw: dict[str, Any] = {"cache_dir": str(cache_dir)}
        try:  # offline once downloaded
            self.tokenizer = AutoTokenizer.from_pretrained(
                model_name, padding_side="left", local_files_only=True, **kw
            )
            model = AutoModelForCausalLM.from_pretrained(model_name, dtype=dtype, local_files_only=True, **kw)
        except OSError:
            log.info("downloading re-ranker model %s (one time, ~1.2 GB)", model_name)
            self.tokenizer = AutoTokenizer.from_pretrained(model_name, padding_side="left", **kw)
            model = AutoModelForCausalLM.from_pretrained(model_name, dtype=dtype, **kw)
        self.model = model.to(self.device).eval()
        self.batch_size = batch_size
        self.yes = self.tokenizer.convert_tokens_to_ids("yes")
        self.no = self.tokenizer.convert_tokens_to_ids("no")
        self.prefix = self.tokenizer.encode(PREFIX, add_special_tokens=False)
        self.suffix = self.tokenizer.encode(SUFFIX, add_special_tokens=False)
        log.info("re-ranker loaded on %s", self.device)

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        out: list[float] = []
        for i in range(0, len(documents), self.batch_size):
            out.extend(self._batch([format_pair(query, d) for d in documents[i : i + self.batch_size]]))
        return out

    def _batch(self, pairs: list[str]) -> list[float]:
        torch = self.torch
        inputs = self.tokenizer(
            pairs,
            padding=False,
            truncation="longest_first",
            return_attention_mask=False,
            max_length=MAX_TOKENS - len(self.prefix) - len(self.suffix),
        )
        inputs["input_ids"] = [self.prefix + ids + self.suffix for ids in inputs["input_ids"]]
        batch = self.tokenizer.pad(inputs, padding=True, return_tensors="pt", max_length=MAX_TOKENS)
        batch = {k: v.to(self.device) for k, v in batch.items()}
        with torch.no_grad():
            logits = self.model(**batch).logits[:, -1, :]
            pair = torch.stack([logits[:, self.no], logits[:, self.yes]], dim=1).float()
            return [float(x) for x in torch.nn.functional.log_softmax(pair, dim=1)[:, 1].exp().tolist()]


def is_cached(model_name: str, cache_dir: Path) -> bool:
    """True once the model has been downloaded into ``cache_dir`` (Hugging Face cache layout)."""
    snapshots = cache_dir / f"models--{model_name.replace('/', '--')}" / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


def configure_hub(model_name: str, cache_dir: Path) -> None:
    """No Hugging Face telemetry ever; fully offline once the model is cached, so loading it
    makes no network calls (background Hub checks also delayed process exit by minutes)."""
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    if is_cached(model_name, cache_dir):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


class FakeReranker:
    """Scores by how many query words a document contains (tests and offline use)."""

    def __init__(self, fail: Exception | None = None, delay: float = 0.0) -> None:
        self.fail, self.delay = fail, delay
        self.calls: list[tuple[str, int]] = []

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        import time

        self.calls.append((query, len(documents)))
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        words = {w.lower() for w in query.split()}
        return [
            sum(w.lower().strip(".,?!") in words for w in d.split()) / (1 + len(d.split())) for d in documents
        ]


class Reranking:
    """Holds the re-ranker: loads it in the background, scores with a timeout.

    ``score`` raises RerankerUnavailableError while the model is loading, if it failed to
    load (e.g. torch not installed), if scoring raised, or if it took longer than ``timeout``.
    A timed-out call keeps running in its worker thread; the next call waits behind it, so a
    stuck model makes later searches fall back quickly rather than pile up threads."""

    def __init__(self, load: Callable[[], Reranker], timeout_seconds: float) -> None:
        self.timeout = timeout_seconds
        self._model: Reranker | None = None
        self._error: str | None = None
        self._loaded = threading.Event()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rerank")
        self._busy: Future[list[float]] | None = None
        threading.Thread(target=self._load, args=(load,), name="rerank-load", daemon=True).start()

    def _load(self, load: Callable[[], Reranker]) -> None:
        try:
            self._model = load()
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            log.warning("re-ranker unavailable: %s", self._error)
        finally:
            self._loaded.set()

    def wait_loaded(self, timeout: float | None = None) -> bool:
        return self._loaded.wait(timeout)

    @property
    def status(self) -> str:
        if not self._loaded.is_set():
            return "loading"
        return "ready" if self._model is not None else f"unavailable ({self._error})"

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        model = self._model
        if model is None:
            raise RerankerUnavailableError(f"re-ranker {self.status}")
        if self._busy is not None and not self._busy.done():
            raise RerankerUnavailableError("re-ranker still busy with an earlier search")
        self._busy = self._worker.submit(model.score, query, list(documents))
        try:
            scores = self._busy.result(timeout=self.timeout)
        except FutureTimeout:
            raise RerankerUnavailableError(f"re-ranker took longer than {self.timeout:g}s") from None
        except Exception as e:
            raise RerankerUnavailableError(f"re-ranker failed: {type(e).__name__}: {e}") from None
        if len(scores) != len(documents):
            raise RerankerUnavailableError("re-ranker returned the wrong number of scores")
        return scores

    def close(self) -> None:
        self._worker.shutdown(wait=False, cancel_futures=True)


def qwen_loader(model_name: str, output_dir: Path) -> Callable[[], Reranker]:
    def load() -> Reranker:  # pragma: no cover - needs torch and the model
        return Qwen3Reranker(model_name, output_dir / "models")

    return load
