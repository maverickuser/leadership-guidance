"""Small text helpers shared by boilerplate detection, chunking and keyword search."""

from __future__ import annotations

import re
import unicodedata

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")
# Letters that NFKD does not decompose into a base letter + accent.
_EXTRA_FOLD = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss",
                             "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i"})  # fmt: skip


def fold(text: str) -> str:
    """Remove accents ("Résumé" -> "Resume"). Used for the BM25 columns and for queries, because
    pg_search stems words before folding accents, which would index "résumé" and "resume"
    differently."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_EXTRA_FOLD))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def words(text: str) -> list[str]:
    return text.split()


def word_count(text: str) -> int:
    return len(text.split())


def squash(text: str) -> str:
    """Collapse all whitespace runs to single spaces."""
    return " ".join(text.split())


def paragraphs(text: str) -> list[str]:
    """Paragraphs of a section (blank-line separated), whitespace-squashed, empties dropped."""
    return [p for p in (squash(part) for part in _PARA_SPLIT_RE.split(text)) if p]


def normalize_for_matching(text: str) -> str:
    """Lowercase, accent-free, punctuation-free, single-spaced: for duplicate detection."""
    return squash(_PUNCT_RE.sub(" ", fold(text).lower()))
