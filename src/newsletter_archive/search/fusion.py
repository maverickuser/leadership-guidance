"""Combine ranked lists with Reciprocal Rank Fusion, and keep results diverse (pure)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

RRF_K = 60


@dataclass(frozen=True)
class Fused:
    passage_id: str
    score: float
    found_by: tuple[str, ...]  # e.g. ("keyword", "semantic")
    best_rank: int  # 1-based, best position in any list


def rrf(lists: Mapping[str, Sequence[str]], k: int = RRF_K) -> list[Fused]:
    """score = Σ 1 / (k + rank). Passages found by both lists rise above those found by one at
    similar positions. Deterministic: ties go to the better single rank, then the passage id."""
    scores: dict[str, float] = {}
    best: dict[str, int] = {}
    found: dict[str, list[str]] = {}
    for name in sorted(lists):
        for rank, pid in enumerate(lists[name], start=1):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (k + rank)
            best[pid] = min(best.get(pid, rank), rank)
            found.setdefault(pid, []).append(name)
    order = sorted(scores, key=lambda p: (-scores[p], best[p], p))
    return [Fused(p, scores[p], tuple(found[p]), best[p]) for p in order]


def diversify(
    ranked: Sequence[str], post_of: Callable[[str], str], is_summary: Callable[[str], bool],
    max_passages: int = 3, max_summaries: int = 1,
) -> list[str]:  # fmt: skip
    """Keep ranked order but at most ``max_passages`` passages and ``max_summaries`` summaries
    per post, so one long post can't crowd out the rest."""
    passages: dict[str, int] = {}
    summaries: dict[str, int] = {}
    out: list[str] = []
    for pid in ranked:
        post = post_of(pid)
        bucket, cap = (summaries, max_summaries) if is_summary(pid) else (passages, max_passages)
        if bucket.get(post, 0) < cap:
            bucket[post] = bucket.get(post, 0) + 1
            out.append(pid)
    return out
