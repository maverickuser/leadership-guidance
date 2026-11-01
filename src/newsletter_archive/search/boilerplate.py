"""Find paragraphs repeated across many posts (welcome intros, sign-offs) and remove them
before chunking (pure; no I/O).

A paragraph of at least ``min_words`` words is boilerplate when it, or a near-duplicate of it,
appears in at least ``min_posts`` distinct posts. Near-duplicates are found with MinHash over
word 5-gram shingles, LSH banding for candidate pairs, an exact Jaccard check, and union-find
to group them. The set is data-driven (no site text in the code) and recomputed every run.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from .models import Section
from .text import normalize_for_matching, paragraphs, word_count

SHINGLE_WORDS = 5
NUM_PERM = 64
BANDS = 16  # 16 bands x 4 rows: pairs above ~0.5 similarity become candidates
ROWS = NUM_PERM // BANDS
_PRIME = (1 << 61) - 1
_rng = np.random.default_rng(20261101)  # fixed: same input, same clusters
_A = _rng.integers(1, 1 << 29, size=NUM_PERM, dtype=np.uint64)
_B = _rng.integers(0, 1 << 29, size=NUM_PERM, dtype=np.uint64)
SAMPLE_CHARS = 200


def paragraph_key(paragraph: str) -> str:
    """Identity of a paragraph for boilerplate matching: hash of its normalised text."""
    return hashlib.sha256(normalize_for_matching(paragraph).encode("utf-8")).hexdigest()


def _shingles(norm: str) -> frozenset[str]:
    ws = norm.split()
    if len(ws) <= SHINGLE_WORDS:
        return frozenset([norm])
    return frozenset(" ".join(ws[i : i + SHINGLE_WORDS]) for i in range(len(ws) - SHINGLE_WORDS + 1))


def _signature(shingles: Iterable[str]) -> np.ndarray:
    # 32-bit shingle hashes keep a*x + b below 2**61, so uint64 arithmetic never overflows.
    x = np.array(
        [int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=4).digest(), "big") for s in shingles],
        dtype=np.uint64,
    )
    return ((np.outer(x, _A) + _B) % _PRIME).min(axis=0)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[max(ri, rj)] = min(ri, rj)


@dataclass(frozen=True)
class BoilerplateCluster:
    cluster_id: int
    keys: tuple[str, ...]  # paragraph_key of every distinct wording in the cluster
    post_count: int
    sample: str


@dataclass(frozen=True)
class CleanResult:
    sections: list[Section]
    removed_paragraphs: int
    removed_words: int
    total_words: int

    @property
    def loss_fraction(self) -> float:
        return self.removed_words / self.total_words if self.total_words else 0.0


@dataclass(frozen=True)
class Boilerplate:
    keys: frozenset[str]
    clusters: tuple[BoilerplateCluster, ...]

    def is_boilerplate(self, paragraph: str) -> bool:
        return paragraph_key(paragraph) in self.keys

    def clean(self, sections: Sequence[Section]) -> CleanResult:
        """Drop boilerplate paragraphs; sections left empty are dropped."""
        out: list[Section] = []
        removed = removed_words = total = 0
        for sec in sections:
            kept: list[str] = []
            for para in paragraphs(sec.text):
                n = word_count(para)
                total += n
                if self.is_boilerplate(para):
                    removed += 1
                    removed_words += n
                else:
                    kept.append(para)
            if kept:
                out.append(Section(sec.heading, "\n\n".join(kept)))
        return CleanResult(out, removed, removed_words, total)


EMPTY = Boilerplate(frozenset(), ())


def find_boilerplate(
    posts: Mapping[str, Sequence[Section]],
    *,
    min_posts: int = 5,
    similarity: float = 0.8,
    min_words: int = 8,
) -> Boilerplate:
    """Boilerplate paragraphs across ``posts`` (post id -> sections)."""
    seen_posts: dict[str, set[str]] = defaultdict(set)
    norms: dict[str, str] = {}
    samples: dict[str, str] = {}
    for post_id in sorted(posts):
        for sec in posts[post_id]:
            for para in paragraphs(sec.text):
                norm = normalize_for_matching(para)
                if len(norm.split()) < min_words:
                    continue
                key = hashlib.sha256(norm.encode("utf-8")).hexdigest()
                seen_posts[key].add(post_id)
                norms.setdefault(key, norm)
                samples.setdefault(key, para)
    keys = sorted(norms)
    if not keys:
        return EMPTY

    shingles = [_shingles(norms[k]) for k in keys]
    signatures = np.stack([_signature(s) for s in shingles])
    uf = _UnionFind(len(keys))
    for band in range(BANDS):
        buckets: dict[bytes, list[int]] = defaultdict(list)
        for i, sig in enumerate(signatures):
            buckets[sig[band * ROWS : (band + 1) * ROWS].tobytes()].append(i)
        for members in buckets.values():
            for a_pos, a in enumerate(members):
                for b in members[a_pos + 1 :]:
                    if uf.find(a) != uf.find(b) and _jaccard(shingles[a], shingles[b]) >= similarity:
                        uf.union(a, b)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(keys)):
        groups[uf.find(i)].append(i)

    clusters: list[BoilerplateCluster] = []
    for root in sorted(groups):
        members = groups[root]
        post_ids = set().union(*(seen_posts[keys[i]] for i in members))
        if len(post_ids) >= min_posts:
            member_keys = tuple(keys[i] for i in members)
            sample = samples[member_keys[0]][:SAMPLE_CHARS]
            clusters.append(BoilerplateCluster(len(clusters) + 1, member_keys, len(post_ids), sample))
    return Boilerplate(frozenset(k for c in clusters for k in c.keys), tuple(clusters))
