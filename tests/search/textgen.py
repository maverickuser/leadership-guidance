"""Deterministic filler text for chunking tests (no real article text)."""

from __future__ import annotations

import random

WORDS = [
    "team",
    "manager",
    "feedback",
    "hiring",
    "goal",
    "plan",
    "review",
    "growth",
    "trust",
    "delegate",
    "meeting",
    "project",
    "career",
    "leader",
    "decision",
    "customer",
    "metric",
    "process",
    "engineer",
    "promotion",
    "coaching",
]


def sentence(rng: random.Random, n: int) -> str:
    ws = [rng.choice(WORDS) for _ in range(n)]
    return " ".join([ws[0].capitalize(), *ws[1:]]) + rng.choice([".", ".", ".", "?", "!"])


def paragraph(rng: random.Random, words: int, sentence_words: int = 10) -> str:
    out, left = [], words
    while left > 0:
        n = min(sentence_words, left)
        out.append(sentence(rng, n))
        left -= n
    return " ".join(out)


def section_text(rng: random.Random, paragraph_words: list[int], sentence_words: int = 10) -> str:
    return "\n\n".join(paragraph(rng, w, sentence_words) for w in paragraph_words)
