"""One GPU at a time (openspec use-local-embeddings, design §4).

The local embedder and the re-ranker run on Apple MPS in the same process. Measured on
20 Nov 2026: when a query embedding and a re-ranking ran at once from different threads, the
MPS command buffer never completed (10 minutes at 0.4% CPU). Every forward pass on a local
model holds ``GPU_LOCK``; a query embedding takes ~50 ms, so the wait is short.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

GPU_LOCK = threading.Lock()
# Loading a model imports transformers lazily, and two threads importing it at once fail
# ("cannot import name 'AutoModelForCausalLM' from 'transformers'": measured 3 of 3 times when
# the MCP server loaded the embedder and the re-ranker together, 20 Nov 2026). Load one at a time.
MODEL_LOAD_LOCK = threading.Lock()
# A re-ranking that timed out keeps running (and holding the lock) in its worker thread. If the
# GPU hangs there, waiting without a limit would hang every later search; give up instead.
GPU_WAIT_SECONDS = 30.0

T = TypeVar("T")
A = TypeVar("A")


class GpuBusyError(RuntimeError):
    """The GPU stayed busy longer than GPU_WAIT_SECONDS."""


def on_gpu(fn: Callable[[A], T], arg: A, wait: float = GPU_WAIT_SECONDS) -> T:
    """Run one forward pass while holding ``GPU_LOCK``; GpuBusyError if it isn't free in time."""
    if not GPU_LOCK.acquire(timeout=wait):
        raise GpuBusyError(f"the GPU was busy for over {wait:g}s (a stuck re-ranking?)")
    try:
        return fn(arg)
    finally:
        GPU_LOCK.release()
