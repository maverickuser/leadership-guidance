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


def rrf(
    lists: Mapping[str, Sequence[str]], k: int = RRF_K, weights: Mapping[str, float] | None = None
) -> list[Fused]:
    """score = Σ weight / (k + rank) (weight 1 unless given). Passages found by both lists rise
    above those found by one at similar positions. Repeats within one list count once.
    Deterministic: ties go to the better single rank, then the passage id."""
    scores: dict[str, float] = {}
    best: dict[str, int] = {}
    found: dict[str, list[str]] = {}
    for name in sorted(lists):
        seen: set[str] = set()
        for rank, pid in enumerate(lists[name], start=1):
            if pid in seen:  # a repeated id counts once, at its first (best) rank
                continue
            seen.add(pid)
            scores[pid] = scores.get(pid, 0.0) + (weights or {}).get(name, 1.0) / (k + rank)
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


def represent_sources(
    ranked: Sequence[str],
    limit: int,
    source_of: Callable[[str], str | None],
    qualifies: Callable[[str], bool],
    sources: Sequence[str],
) -> list[str]:
    """The first ``limit`` of ``ranked``, changed so every newsletter in ``sources`` with a
    qualifying result below the cut gets one place (spec hybrid-search / Result content).

    For each newsletter missing from the top: its best result below the cut, if it qualifies,
    replaces the lowest result of a newsletter that has more than one in the top. The kept
    results stay in their original order, so a swapped-in result shows its own (lower) rank and
    score; nothing is disguised. A newsletter with nothing that qualifies is not forced in."""
    top = list(ranked[:limit])
    position = {pid: i for i, pid in enumerate(ranked)}
    for src in sources:
        if any(source_of(p) == src for p in top):
            continue
        best = next((p for p in ranked[limit:] if source_of(p) == src), None)
        if best is None or not qualifies(best):
            continue
        counts: dict[str | None, int] = {}
        for p in top:
            counts[source_of(p)] = counts.get(source_of(p), 0) + 1
        victim = next((p for p in reversed(top) if counts[source_of(p)] > 1), None)
        if victim is None:
            continue
        top[top.index(victim)] = best
    return sorted(top, key=lambda p: position[p])


# -- post-level selection (openspec improve-search-post-selection, lld §2) ------------------------

POST_SCORE_TOP = 3  # sum_top3: a post's score is the sum of its 3 best rows' fused scores


@dataclass(frozen=True)
class PostGroup:
    """One post's fused rows: its score, its passages (best first) and its summary row."""

    post_id: str
    score: float
    passages: tuple[str, ...]
    summary: str | None
    best_rank: int  # 1-based position of the post's best row in the fused list (tie-break)


def group_by_post(
    fused: Sequence[Fused],
    post_of: Callable[[str], str],
    is_summary: Callable[[str], bool],
    mode: str = "max",
) -> list[PostGroup]:
    """The fused rows grouped by post, best post first. ``mode`` "max" scores a post by its best
    row, "sum_top3" by the sum of its 3 best rows (rewarding posts with several matching parts).
    Ties: the post whose best row comes first, then the post id."""
    if mode not in ("max", "sum_top3"):
        raise ValueError(f"unknown post score {mode!r}")
    rows: dict[str, list[tuple[int, Fused]]] = {}
    for i, f in enumerate(fused, start=1):
        rows.setdefault(post_of(f.passage_id), []).append((i, f))
    groups = []
    for post, items in rows.items():
        best = sorted((f.score for _, f in items), reverse=True)
        score = best[0] if mode == "max" else sum(best[:POST_SCORE_TOP])
        passages = tuple(f.passage_id for _, f in items if not is_summary(f.passage_id))
        summary = next((f.passage_id for _, f in items if is_summary(f.passage_id)), None)
        groups.append(PostGroup(post, score, passages, summary, items[0][0]))
    return sorted(groups, key=lambda g: (-g.score, g.best_rank, g.post_id))


def rerank_batch(groups: Sequence[PostGroup], rows_per_post: int, with_summaries: bool) -> list[str]:
    """The rows to re-rank: each group's best ``rows_per_post`` passages, then its summary
    (when ``with_summaries``), in group order."""
    out: list[str] = []
    for g in groups:
        out += g.passages[:rows_per_post]
        if with_summaries and g.summary is not None:
            out.append(g.summary)
    return list(dict.fromkeys(out))


def select_across_posts(
    ranked: Sequence[str],
    post_of: Callable[[str], str],
    is_summary: Callable[[str], bool],
    max_passages: int = 3,
    max_summaries: int = 1,
) -> list[str]:
    """Every allowed item in pick order: first each post's best item (in ``ranked`` order), then
    each post's next-best item, and so on, with at most ``max_passages`` passages and
    ``max_summaries`` summaries per post. Not cut to a limit, so callers can look below it."""
    queues: dict[str, list[str]] = {}
    for pid in ranked:
        queues.setdefault(post_of(pid), []).append(pid)
    position = {pid: i for i, pid in enumerate(ranked)}
    taken: dict[str, list[int]] = {post: [0, 0] for post in queues}  # passages, summaries
    out: list[str] = []
    while True:
        round_items: list[str] = []
        for post, queue in queues.items():
            while queue:
                pid = queue.pop(0)
                kind = 1 if is_summary(pid) else 0
                cap = max_summaries if kind else max_passages
                if taken[post][kind] < cap:
                    taken[post][kind] += 1
                    round_items.append(pid)
                    break
        if not round_items:
            return out
        out += sorted(round_items, key=lambda p: position[p])
