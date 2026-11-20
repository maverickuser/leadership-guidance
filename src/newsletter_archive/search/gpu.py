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

T = TypeVar("T")
A = TypeVar("A")


def on_gpu(fn: Callable[[A], T], arg: A) -> T:
    """Run one forward pass while holding ``GPU_LOCK``."""
    with GPU_LOCK:
        return fn(arg)
