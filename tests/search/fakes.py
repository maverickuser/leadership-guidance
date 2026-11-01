"""Fakes for the Gemini SDK and embedders. No network."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
from google.genai import errors as genai_errors

GOOD_SUMMARY = " ".join(["The post argues that managers should delegate work and grow their team."] * 9)


def fake_vector(text: str, dim: int) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    return np.random.default_rng(seed).standard_normal(dim).astype(np.float32).tolist()


def api_error(code: int, retry_after: str | None = None) -> genai_errors.APIError:
    response = httpx.Response(code, headers={"retry-after": retry_after} if retry_after else {})
    body = {"error": {"code": code, "message": f"fake {code}", "status": "FAKE"}}
    cls = genai_errors.ClientError if code < 500 else genai_errors.ServerError
    return cls(code, body, response)


def generate_response(text: str, finish: str = "STOP", thoughts: int = 5) -> Any:
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
        usage_metadata=SimpleNamespace(
            prompt_token_count=100, candidates_token_count=50, thoughts_token_count=thoughts
        ),
    )


class FakeModels:
    def __init__(self, sdk: FakeSDK) -> None:
        self.sdk = sdk

    def embed_content(self, model: str, contents: Any, config: Any) -> Any:
        self.sdk.calls.append({"model": model, "contents": contents, "config": config})
        if self.sdk.script:
            raise self.sdk.script.pop(0)
        texts = [c.parts[0].text for c in contents]
        dim = self.sdk.dim or getattr(config, "output_dimensionality", 4)
        vectors = [fake_vector(t, dim) for t in texts] + [fake_vector("x", dim)] * self.sdk.extra_vectors
        return SimpleNamespace(embeddings=[SimpleNamespace(values=v) for v in vectors])

    def generate_content(self, model: str, contents: Any, config: Any) -> Any:
        self.sdk.calls.append({"model": model, "contents": contents, "config": config})
        if self.sdk.gen_script:
            raise self.sdk.gen_script.pop(0)
        return self.sdk.answers.pop(0) if self.sdk.answers else generate_response(GOOD_SUMMARY)


class FakeSDK:
    """Stands in for google.genai.Client. ``script`` / ``gen_script`` hold exceptions for the next
    embedding / generation calls; ``answers`` holds generation responses (then GOOD_SUMMARY)."""

    def __init__(
        self,
        script: Sequence[BaseException] = (),
        dim: int | None = None,
        extra_vectors: int = 0,
        answers: Sequence[Any] = (),
        gen_script: Sequence[BaseException] = (),
    ):
        self.script = list(script)
        self.gen_script = list(gen_script)
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []
        self.dim = dim
        self.extra_vectors = extra_vectors
        self.models = FakeModels(self)


class FakeEmbedder:
    """Deterministic vectors; records every batch it is asked to embed."""

    def __init__(self, model: str = "fake-model", dim: int = 8, batch_size: int = 50, fail_on_batch=None):
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self.batches: list[list[str]] = []
        self.fail_on_batch = fail_on_batch  # 1-based batch number that raises

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        self.batches.append(list(texts))
        if self.fail_on_batch == len(self.batches):
            raise RuntimeError("batch failed")
        out = []
        for t in texts:
            v = np.asarray(fake_vector(t, self.dim), dtype=np.float32)
            out.append(v / np.linalg.norm(v))
        return out
